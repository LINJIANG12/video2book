# -*- coding: utf-8 -*-
"""块级音频与逐字稿契约（装箱 / 劈分 / 命名 / 切分 / 文件系统降级）。

「块即模块」是阶段一的主链路：装箱决定的块边界同时是长文、教材与笔记的模块边界，
所以这里钉的都是**机器契约**，而不是实现细节：

1. 装箱以**集**为最小单位，块内连续、不重不漏、不越取音硬上限；
2. 块时长必须可配置（写死会让 `--block-minutes` / 环境变量失效），目标超上限时夹紧；
3. 只有超长单集才劈分，且劈完任一段低于容差就不劈（61 分钟不劈、76 分钟劈成上下两段）；
4. 块音频/块级稿的命名只取决于块结构，含劈分腿时用 `P12上-P13下` 表达，绝不与分集稿同名；
5. 逐字稿切分只在时间戳可信时才是机械的：边界可疑时整块标 suspect、一集都不放行；
6. 文件系统里不可访问的条目必须降级为「跳过」，单个坏链接不得让整轮扫描集体失明。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from conftest import pad_to, write_text

from src.core import fsutil
from src.core.audio_merger import (
    ENV_BLOCK_MINUTES,
    ENV_ONESHOT_LIMIT_MINUTES,
    AudioMerger,
)
from src.core.quality_gate import collect_markdown
from src.core.transcript_splitter import TranscriptSplitter
from src.core.workspace import TaskWorkspace


# ---------------------------------------------------------------------------
# 构造数据的就地辅助（原脚本里的 `_records` / `_write_blocks` 之类）
# ---------------------------------------------------------------------------

def _records(durations_sec: Sequence[float]) -> List[Dict[str, Any]]:
    """构造 `plan_units` 的输入：逐集媒体记录（只有集号与实测时长会被用到）。"""
    return [
        {
            "page": index + 1,
            "duration": float(duration),
            "path": f"P{index + 1:02d}.m4a",
            "title": f"第{index + 1}集",
            "size": 1,
            "codec": "aac",
            "sample_rate": 16000,
            "channels": 1,
        }
        for index, duration in enumerate(durations_sec)
    ]


# 装箱样本：7×10 分钟 + 1×30 分钟 + 4×5 分钟 = 120 分钟（区间 40–60，硬上限 75）
PACK_DURATIONS = [600.0] * 7 + [1800.0] + [300.0] * 4

# 时间表样本（块内两集，各 10 分钟）
SEGMENTS = [
    {"page": 1, "start_sec": 0.0, "end_sec": 600.0, "start": "00:00:00", "end": "00:10:00"},
    {"page": 2, "start_sec": 600.0, "end_sec": 1200.0, "start": "00:10:00", "end": "00:20:00"},
]
TEXT_WITH_STAMPS = (
    "[00:00:00] 第一集开场\n"
    "[00:05:00] 第一集中段\n"
    "[00:10:00] 第二集开场\n"
    "[00:15:00] 第二集中段\n"
)
# 交界（00:10:00）附近没有任何时间戳：该处切分只能靠插值推定
TEXT_THIN_ANCHORS = "[00:00:00] 开场\n[00:15:00] 交界附近没有时间戳\n"

# 稀疏标注样本：P19 整集没有时间戳，正文会一路归到 P18 名下
SPARSE_SEGMENTS = [
    {"page": 18, "start_sec": 0.0, "end_sec": 600.0, "start": "00:00:00", "end": "00:10:00",
     "duration_sec": 600.0},
    {"page": 19, "start_sec": 600.0, "end_sec": 1200.0, "start": "00:10:00", "end": "00:20:00",
     "duration_sec": 600.0},
    {"page": 20, "start_sec": 1200.0, "end_sec": 1800.0, "start": "00:20:00", "end": "00:30:00",
     "duration_sec": 600.0},
]
SPARSE_TITLES = {18: "十八", 19: "十九", 20: "二十"}


def _sparse_text() -> str:
    """P18 一条时间戳之后跟着 29 行属于 P19 的无时间戳正文，再到 P20。"""
    return (
        "[00:00:00] P18 开场\n"
        + "\n".join(f"这是实际属于 P19 的无时间戳正文第 {i} 行" for i in range(1, 30))
        + "\n[00:20:00] P20 开场\n"
    )


RELIABLE_TEXT = (
    "[00:00:00] P18 正确内容\n"
    "[00:10:00] P19 正确内容\n"
    "[00:20:00] P20 正确内容\n"
)


@pytest.fixture
def suspect_ws(make_workspace):
    """一个有污染稿的工作区 + 一份边界不可信的块（P18–P20）。"""
    ws = make_workspace("疑点切分_BVTEST01")
    block = {"block_id": 3, "episodes": [18, 19, 20], "segments": SPARSE_SEGMENTS}
    stale = TranscriptSplitter.episode_path(ws, 18, "十八")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("上一轮的污染稿", encoding="utf-8")
    return ws, block, stale


@pytest.fixture
def fixed_split(suspect_ws):
    """先做一次可疑切分（留下 suspect 标记与污染稿），再用可靠时间戳重切。"""
    ws, block, stale = suspect_ws
    TranscriptSplitter.write_episode_transcripts(
        ws, block, _sparse_text(), titles=SPARSE_TITLES
    )
    fixed = TranscriptSplitter.write_episode_transcripts(
        ws, block, RELIABLE_TEXT, titles=SPARSE_TITLES
    )
    return ws, block, stale, fixed


# ===========================================================================
# 1) 块时长口径：默认值生效、可配置、超上限夹紧、非法值回退
# ===========================================================================

def test_block_minutes_defaults_to_fifty():
    """默认目标 50 分钟（区间中段）。"""
    assert AudioMerger.block_minutes() == 50.0


def test_limits_expose_target_ceiling_and_band():
    """对外播报的三元组：目标 50、硬上限 75、生效值 50，区间 40–60。"""
    limits = AudioMerger.limits()
    assert (limits["target"], limits["ceiling"], limits["effective"]) == (50.0, 75.0, 50.0)
    assert (limits["min"], limits["max"]) == (40.0, 60.0)


def test_block_minutes_follows_env_override(monkeypatch: pytest.MonkeyPatch):
    """块时长必须可覆盖：写死会让 `--block-minutes` 与环境变量一起失效。"""
    monkeypatch.setenv(ENV_BLOCK_MINUTES, "45")
    assert AudioMerger.block_minutes() == 45.0
    assert AudioMerger.limits()["effective"] == 45.0


def test_target_is_clamped_to_oneshot_ceiling(monkeypatch: pytest.MonkeyPatch):
    """目标超过取音侧硬上限时，生效值必须夹紧到上限（否则整片会被自动分卷）。"""
    monkeypatch.setenv(ENV_BLOCK_MINUTES, "45")
    monkeypatch.setenv(ENV_ONESHOT_LIMIT_MINUTES, "30")
    assert AudioMerger.limits()["effective"] == 30.0


def test_non_positive_block_minutes_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    """非正数配置回退默认，工具层不因配置笔误中断。"""
    monkeypatch.setenv(ENV_BLOCK_MINUTES, "0")
    assert AudioMerger.block_minutes() == 50.0


def test_malformed_block_minutes_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    """非法配置回退默认，工具层不因配置笔误中断。"""
    monkeypatch.setenv(ENV_BLOCK_MINUTES, "abc")
    assert AudioMerger.block_minutes() == 50.0


# ===========================================================================
# 2) 装箱：不重不漏、块内连续、绝大多数落进区间、硬上限不越
# ===========================================================================

def test_pack_covers_every_episode_exactly_once():
    """装箱后集号既不重复也不遗漏，否则有集永远不会被转录。"""
    groups = AudioMerger.pack(PACK_DURATIONS, 50.0, 60.0, 40.0)
    flat = sorted(index for group in groups for index in group)
    assert flat == list(range(len(PACK_DURATIONS)))


def test_pack_keeps_blocks_contiguous():
    """块内必须是连续的几集：转录出来的文本才是一段连续讲解。"""
    groups = AudioMerger.pack(PACK_DURATIONS, 50.0, 60.0, 40.0)
    for group in groups:
        assert group == list(range(group[0], group[0] + len(group)))


def test_pack_never_exceeds_hard_ceiling():
    """任何块都不得越过取音硬上限（越过即触发分卷续读，调用次数反而回升）。"""
    groups = AudioMerger.pack(PACK_DURATIONS, 50.0, 60.0, 40.0)
    for group in groups:
        span = sum(PACK_DURATIONS[index] for index in group)
        assert span <= 75.0 * 60 + 1e-6


def test_pack_keeps_almost_all_blocks_in_band():
    """绝大多数块要落进 40–60 分钟区间：允许恰有一块受余量限制出界。"""
    groups = AudioMerger.pack(PACK_DURATIONS, 50.0, 60.0, 40.0)
    in_band = 0
    for group in groups:
        span = sum(PACK_DURATIONS[index] for index in group)
        if 40.0 * 60 - 1 <= span <= 60.0 * 60 + 1:
            in_band += 1
    assert in_band >= len(groups) - 1


def test_oversized_episode_is_packed_alone():
    """本身超过上限的单集绝不能与别集同块。"""
    groups = AudioMerger.pack([5000.0, 600.0], 50.0, 60.0, 40.0)
    assert any(len(group) == 1 and 0 in group for group in groups)


# ===========================================================================
# 3) 装箱单元：超长集劈上下两半，劈完不合格就不劈
# ===========================================================================

def test_long_episode_is_split_into_upper_and_lower_legs():
    """102 分钟的单集必须劈成「上/下」两条腿，否则整块超出取音阈值。"""
    units = AudioMerger.plan_units(_records([600.0] * 5 + [6136.0]), 40.0, 60.0)
    labels = [unit["label"] for unit in units]
    assert "P06上" in labels and "P06下" in labels


def test_split_leg_duration_stays_near_band():
    """劈出来的每一腿都要接近区间（这里允许比下限再低 10% 的容差）。"""
    units = AudioMerger.plan_units(_records([600.0] * 5 + [6136.0]), 40.0, 60.0)
    for unit in units:
        if unit["split"]:
            assert 36.0 * 60 - 1 <= unit["duration_sec"] <= 60.0 * 60 + 1


def test_moderate_oversize_episode_is_not_split():
    """61 分钟劈成 30.5+30.5 是两块都不合格，所以宁可整块不劈。"""
    units = AudioMerger.plan_units(_records([61 * 60.0]), 40.0, 60.0)
    assert len(units) == 1 and not units[0]["split"]


def test_episode_over_ceiling_is_split_into_two():
    """76 分钟应劈成两段（各约 38 分钟）。"""
    units = AudioMerger.plan_units(_records([76 * 60.0]), 40.0, 60.0)
    assert len(units) == 2 and all(unit["split"] for unit in units)


def test_episode_within_ceiling_is_never_split():
    """没超上限的集一律不劈——锯开一集是有代价的。"""
    units = AudioMerger.plan_units(_records([30 * 60.0, 25 * 60.0]), 40.0, 60.0)
    assert len(units) == 2 and not any(unit["split"] for unit in units)


# ===========================================================================
# 4) 命名：块音频与覆盖范围
# ===========================================================================

def test_block_filename_carries_course_index_title_and_span():
    """块音频名 = 课程短名_序号_语义标题(覆盖范围)，它同时是人读的模块标识。"""
    assert AudioMerger.block_filename("3小时前端入门教程", 1, "HTML入门与常用标签", "P01-P05") == \
        "3小时前端入门教程_01_HTML入门与常用标签(P01-P05).m4a"


def test_block_filename_falls_back_when_title_missing():
    """缺语义标题时退化为「课程短名_序号(覆盖范围)」，绝不出现空名。"""
    assert AudioMerger.block_filename("课程", 2, "", "P06-P09") == "课程_02(P06-P09).m4a"


def test_block_span_expresses_split_legs():
    """含劈分腿的块按「Pxx上/下」表达覆盖范围。"""
    assert AudioMerger.block_span({"units": [{"label": "P12上"}, {"label": "P13下"}]}) == "P12上-P13下"


def test_block_span_falls_back_to_episode_range():
    """没有装箱单元的旧清单按集号区间表达覆盖范围。"""
    assert AudioMerger.block_span({"episodes": [1, 6]}) == "P01-P06"


# ===========================================================================
# 5) 逐字稿切分：时间戳解析、归属、锚定统计、无时间戳降级
# ===========================================================================

def test_parse_stamp_accepts_all_supported_shapes():
    """`MM:SS` / `HH:MM:SS` / 纯秒数都要认；解析不出来就只能降级为不切。"""
    assert TranscriptSplitter.parse_stamp("05:20") == 320.0
    assert TranscriptSplitter.parse_stamp("00:05:20") == 320.0
    assert TranscriptSplitter.parse_stamp("320") == 320.0


def test_split_assigns_lines_by_timestamp():
    """有时间戳时切分是机械的：每行归到最后一个 start<=t 的集。"""
    outcome = TranscriptSplitter.split(TEXT_WITH_STAMPS, SEGMENTS)
    assert outcome["mode"] == "timestamp"
    assert outcome["buckets"][1] == ["第一集开场", "第一集中段"]
    assert outcome["buckets"][2] == ["第二集开场", "第二集中段"]


def test_split_counts_anchored_boundaries():
    """交界处有时间戳 → 边界被锚定，这条数进统计供门禁判断切分可信度。"""
    outcome = TranscriptSplitter.split(TEXT_WITH_STAMPS, SEGMENTS)
    assert outcome["stats"]["anchored_boundaries"] == 1


def test_split_without_timestamps_degrades_to_unsplit():
    """无时间戳时必须降级为 unsplit，绝不按位置硬切（错位文本比不切更危险）。"""
    outcome = TranscriptSplitter.split("没有任何时间戳的正文", SEGMENTS)
    assert outcome["mode"] == "unsplit"


def test_split_reports_missing_anchor_at_boundary():
    """交界附近没有时间戳时必须如实报 0，否则错位切分会蒙混过关。"""
    outcome = TranscriptSplitter.split(TEXT_THIN_ANCHORS, SEGMENTS)
    assert outcome["stats"]["anchored_boundaries"] == 0


# ===========================================================================
# 6) 稀疏标注的过度归属：整块隔离，一集都不放行
# ===========================================================================

def test_sparse_timestamps_mark_episode_over_assigned():
    """P19 整集没有时间戳 → 内容被 P18 吃掉，必须报出过度归属的集号。"""
    outcome = TranscriptSplitter.split(_sparse_text(), SPARSE_SEGMENTS)
    assert [item["page"] for item in outcome["stats"]["over_assigned"]] == [18]


def test_sparse_timestamps_emit_diagnosis():
    """过度归属要有人读的诊断行，否则只会静默产出错位语料。"""
    outcome = TranscriptSplitter.split(_sparse_text(), SPARSE_SEGMENTS)
    assert any("分到" in line and "P18" in line for line in outcome["diag"])


def test_suspect_split_releases_no_episode_transcript(suspect_ws):
    """边界不可信时整块不得放行任何分集稿——错归属可能污染多个相邻集。"""
    ws, block, _stale = suspect_ws
    outcome = TranscriptSplitter.write_episode_transcripts(
        ws, block, _sparse_text(), titles=SPARSE_TITLES
    )
    assert outcome["status"] == "suspect"
    assert outcome["files"] == {}


def test_suspect_split_marks_every_page_in_block(suspect_ws):
    """suspect 标记覆盖整块的所有集号（不是只标可疑的那一集）。"""
    ws, block, _stale = suspect_ws
    outcome = TranscriptSplitter.write_episode_transcripts(
        ws, block, _sparse_text(), titles=SPARSE_TITLES
    )
    assert outcome["suspect_pages"] == [18, 19, 20]


def test_suspect_split_keeps_existing_transcript_for_diagnosis(suspect_ws):
    """已有文件保留供排障，不得因为不可信就删掉。"""
    ws, block, stale = suspect_ws
    TranscriptSplitter.write_episode_transcripts(
        ws, block, _sparse_text(), titles=SPARSE_TITLES
    )
    assert stale.exists()


def test_suspect_marker_hides_existing_transcript(suspect_ws):
    """带 suspect 标记的旧文件不得被当作可用语料派发。"""
    ws, block, _stale = suspect_ws
    TranscriptSplitter.write_episode_transcripts(
        ws, block, _sparse_text(), titles=SPARSE_TITLES
    )
    assert TranscriptSplitter.existing_episode_transcript(ws, 18, "十八") is None


def test_reliable_split_clears_suspect_status(fixed_split):
    """拿到可靠时间戳重切后必须恢复 split，并交出全部三集。"""
    _ws, _block, _stale, fixed = fixed_split
    assert fixed["status"] == "split"
    assert sorted(fixed["files"]) == [18, 19, 20]


def test_reliable_split_overwrites_polluted_transcript(fixed_split):
    """suspect 解除后必须用可靠切分覆盖旧污染稿。"""
    _ws, _block, stale, _fixed = fixed_split
    assert "上一轮的污染稿" not in stale.read_text(encoding="utf-8")


def test_reliable_split_removes_suspect_marker(fixed_split):
    """可靠重切后 suspect 标记必须解除，否则这一集永远派发不出去。"""
    ws, _block, _stale, _fixed = fixed_split
    assert not TranscriptSplitter.suspect_path(ws, 18, "十八").exists()


# ===========================================================================
# 7) 命名与复用优先级
# ===========================================================================

def test_episode_transcript_path_has_page_prefix(make_workspace):
    """分集逐字稿的正式路径：`subtitles/PXX_<标题>_逐字稿.md`。"""
    ws = make_workspace("命名契约_BVTEST01")
    assert TranscriptSplitter.episode_path(ws, 3, "绪论").name == "P03_绪论_逐字稿.md"


def test_block_transcript_path_uses_block_structure(make_workspace):
    """清单缺块号/覆盖范围时退回按块音频文件名取名（兼容手写旧清单）。"""
    ws = make_workspace("命名契约_BVTEST01")
    block = {"audio": "audio/_blocks/BLK02_P13-P17.m4a"}
    assert TranscriptSplitter.block_path(ws, block).name == "BLK02_P13-P17_逐字稿.md"


def test_missing_episode_transcript_is_none(make_workspace):
    """尚无分集稿时返回 None，调用方据此派发转录。"""
    ws = make_workspace("命名契约_BVTEST01")
    assert TranscriptSplitter.existing_episode_transcript(ws, 3, "绪论") is None


def test_legacy_clean_txt_is_not_reused(make_workspace):
    """`_clean.txt` 曾是伪逐字稿混进流水线的放行口，顶着这个名字的文本一律不认。"""
    ws = make_workspace("命名契约_BVTEST01")
    write_text(ws.subtitles_dir / "P03_绪论_clean.txt", "来路不明的一段文本")
    assert TranscriptSplitter.existing_episode_transcript(ws, 3, "绪论") is None


def test_split_episode_transcript_is_reused(make_workspace):
    """已切出的分集逐字稿应被认作可用语料。"""
    ws = make_workspace("命名契约_BVTEST01")
    fresh = write_text(TranscriptSplitter.episode_path(ws, 3, "绪论"), "块级转录产物")
    assert TranscriptSplitter.existing_episode_transcript(ws, 3, "绪论") == fresh


def test_block_transcript_is_named_by_block_structure(make_workspace):
    """无收益装箱（每块仅一集）时块级稿仍按块结构命名，不跟分集稿混。"""
    ws = make_workspace("命名契约_BVTEST01")
    noop_block = {"block_id": 1, "episodes": [8], "audio": "audio/P08_测试集.m4a"}
    assert TranscriptSplitter.block_path(ws, noop_block).name == "BLK01_P08_逐字稿.md"


def test_block_transcript_never_collides_with_episode_transcript(make_workspace):
    """块级稿与分集稿同名会互相覆盖，命名必须结构性地区分开。"""
    ws = make_workspace("命名契约_BVTEST01")
    noop_block = {"block_id": 1, "episodes": [8], "audio": "audio/P08_测试集.m4a"}
    assert TranscriptSplitter.block_path(ws, noop_block).name != \
        TranscriptSplitter.episode_path(ws, 8, "测试集").name


# ===========================================================================
# 8) 陈旧派发物清理：改块时长后旧任务书必须作废
# ===========================================================================

@pytest.fixture
def orphan_ws(make_workspace):
    """一个块（BLK01_P08）的任务书 + 一份已被装箱淘汰的旧块任务书与旧块稿。"""
    ws = make_workspace("陈旧派发物_BVTEST01")
    write_text(ws.subtitles_dir / "BLK01_P08_转录任务书.md", "当前")
    write_text(ws.subtitles_dir / "BLK09_P70-P79_转录任务书.md", "陈旧")
    write_text(ws.subtitles_dir / "BLK09_P70-P79_逐字稿.md", "陈旧数据")
    return ws


NOOP_BLOCK = {"block_id": 1, "episodes": [8], "audio": "audio/P08_测试集.m4a"}


def test_prune_orphans_removes_superseded_taskbook(orphan_ws):
    """旧编号的任务书是「可被派发的幽灵任务」，必须作废（照着它转录会去读不存在的块）。"""
    stale = AudioMerger.prune_orphans(orphan_ws, [NOOP_BLOCK])
    assert stale["removed_tasks"] == ["BLK09_P70-P79_转录任务书.md"]
    assert not (orphan_ws.subtitles_dir / "BLK09_P70-P79_转录任务书.md").exists()


def test_prune_orphans_keeps_current_taskbook(orphan_ws):
    """当前装箱对应的任务书不得被误删。"""
    AudioMerger.prune_orphans(orphan_ws, [NOOP_BLOCK])
    assert (orphan_ws.subtitles_dir / "BLK01_P08_转录任务书.md").exists()


def test_prune_orphans_reports_orphan_transcripts(orphan_ws):
    """孤立的块级逐字稿要被报告出来，便于人工决定是否重转录。"""
    stale = AudioMerger.prune_orphans(orphan_ws, [NOOP_BLOCK])
    assert stale["orphan_transcripts"] == ["BLK09_P70-P79_逐字稿.md"]


def test_prune_orphans_keeps_orphan_transcript_data(orphan_ws):
    """块级逐字稿是数据产物，只报告不删除（删了会连累已切出的分集稿）。"""
    AudioMerger.prune_orphans(orphan_ws, [NOOP_BLOCK])
    assert (orphan_ws.subtitles_dir / "BLK09_P70-P79_逐字稿.md").exists()


# ===========================================================================
# 9) 工作区与渲染门禁：逐字稿算语料，但不算交付物
# ===========================================================================

def test_transcript_only_workspace_is_not_a_shell(make_workspace):
    """只有逐字稿、还没写长文的工作区不是空壳，不能重建目录丢掉既有语料。"""
    ws = make_workspace("只有逐字稿_BVTEST01")
    write_text(ws.subtitles_dir / "P01_绪论_逐字稿.md", "语料")
    assert TaskWorkspace._populated(ws.root_dir) is True


def test_block_transcript_only_workspace_is_not_a_shell(make_workspace):
    """只有**块级**稿的工作区同样不是空壳。

    第二阶段 A7：`_populated` 的通配曾是 `P*_逐字稿.md`，漏掉了块级稿
    `BLKxx_*_逐字稿.md`——而块稿才是块级链路真正的语料，于是这类工作区会被当成空壳
    重建目录，把稿子丢在外面。
    """
    ws = make_workspace("只有块稿_BVTEST01")
    write_text(ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md", "语料")
    assert TaskWorkspace._populated(ws.root_dir) is True


def test_legacy_clean_txt_only_workspace_is_a_shell(make_workspace):
    """只有历史 `_clean.txt` 的工作区**不算**有料。

    第二阶段 A7：`_populated` 曾把 `subtitles/P*_clean.txt` 计为真语料，而
    `TranscriptSplitter` 拒绝复用它 → 工作区永远被判定为「已有内容」而被复用，
    却永远推不动（僵尸工作区），反复重跑也不会有进展。
    """
    ws = make_workspace("僵尸工作区_BVTEST01")
    write_text(ws.subtitles_dir / "P03_绪论_clean.txt", "来路不明的一段文本")
    assert TaskWorkspace._populated(ws.root_dir) is False


def test_empty_transcript_is_not_reusable_corpus(make_workspace):
    """0 字节的 `_逐字稿.md` 不算语料（两处判定共用 `is_reusable_transcript`）。"""
    ws = make_workspace("空稿_BVTEST01")
    write_text(ws.subtitles_dir / "BLK01_P01_逐字稿.md", "")
    assert TranscriptSplitter.is_reusable_transcript(
        ws.subtitles_dir / "BLK01_P01_逐字稿.md"
    ) is False
    assert TaskWorkspace._populated(ws.root_dir) is False


def test_populated_and_splitter_agree_on_reusable_corpus(make_workspace):
    """两处判定必须**同源**：`_populated` 认的语料，`existing_episode_transcript` 也得认。

    这正是 A7 的根因——两套「什么算可复用语料」的判定漂移了。
    """
    ws = make_workspace("同源判定_BVTEST01")
    fresh = write_text(TranscriptSplitter.episode_path(ws, 3, "绪论"), "块级转录产物")
    assert TranscriptSplitter.is_reusable_transcript(fresh) is True
    assert TaskWorkspace._populated(ws.root_dir) is True

    write_text(ws.subtitles_dir / "P04_绪论_clean.txt", "来路不明")
    assert TranscriptSplitter.is_reusable_transcript(
        ws.subtitles_dir / "P04_绪论_clean.txt"
    ) is False


def test_render_gate_excludes_transcripts_and_taskbooks(make_workspace):
    """逐字稿与任务书不是交付物：ASR 文本里围栏不闭合属正常，不能算渲染缺陷。"""
    ws = make_workspace("渲染门禁_BVTEST01")
    write_text(ws.subtitles_dir / "BLK01_P01_逐字稿.md", pad_to("转录正文"))
    write_text(ws.subtitles_dir / "BLK01_P01_转录任务书.md", pad_to("任务书"))
    keep = write_text(ws.articles_dir / "模块01_绪论_精读长文.md", pad_to("长文正文"))
    assert collect_markdown(ws) == [keep]


def test_blocks_manifest_path_is_stored_relative(make_workspace):
    """块清单路径未登记进路径键时，机器绝对路径会被原样写进 manifest（换机即失效）。"""
    ws = make_workspace("清单便携_BVTEST01")
    manifest_file = AudioMerger.manifest_path(ws)
    ws.save_manifest({"blocks_manifest": str(manifest_file)})
    stored = ws.load_manifest()["blocks_manifest"]
    assert stored == TaskWorkspace.to_relative(manifest_file)


# ===========================================================================
# 10) 真跑 ffmpeg：拼接正确性与幂等（不依赖任何真实语料）
# ===========================================================================

def _synth_sine(path: Path, seconds: int = 2) -> None:
    """用 lavfi 合成一段正弦音，作为块拼接的输入。"""
    ffmpeg_bin = shutil.which("ffmpeg")
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-acodec", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
    )
    assert proc.returncode == 0 and path.exists(), f"合成测试音频失败: {proc.stderr[:200]}"


@pytest.fixture
def merged_block(make_workspace, has_ffmpeg, tmp_path):
    """两集 2 秒正弦音真跑一次块拼接；缺 ffmpeg 时整组用例跳过（has_ffmpeg fixture 判定）。"""
    if not has_ffmpeg:
        pytest.skip("未安装 ffmpeg/ffprobe，跳过块拼接与幂等的真跑验证")
    ws = make_workspace("块拼接_BVTEST01")
    staging = tmp_path / "src_audio"
    staging.mkdir(parents=True, exist_ok=True)
    for page in (11, 12):
        clip = staging / f"P{page}_测试集{page}.m4a"
        _synth_sine(clip)
        (ws.audio_dir / clip.name).write_bytes(clip.read_bytes())
    ws.save_parts([{"page": 11, "title": "测试集11"}, {"page": 12, "title": "测试集12"}])
    first = AudioMerger.merge(ws, [11, 12], target_minutes=1)
    manifest_file = AudioMerger.manifest_path(ws)
    return ws, first, manifest_file.read_bytes()


@pytest.mark.ffmpeg
def test_merge_bundles_two_episodes_into_one_block(merged_block):
    """两集短音频应合成一块并覆盖两集（块是转录的最小单位）。"""
    _ws, first, _bytes = merged_block
    assert first["status"] in ("merged", "noop"), first.get("diag")
    assert len(first["blocks"]) == 1
    assert first["blocks"][0]["episodes"] == [11, 12]


@pytest.mark.ffmpeg
def test_merge_block_has_one_segment_per_episode(merged_block):
    """段表按集落一条，转录后靠它把块内文本切回各集。"""
    _ws, first, _bytes = merged_block
    assert len(first["blocks"][0]["segments"]) == 2


@pytest.mark.ffmpeg
def test_merge_block_segment_table_matches_audio_duration(merged_block):
    """段表时长之和必须等于块音频时长，否则逐字稿切分会整体错位。"""
    _ws, first, _bytes = merged_block
    block = first["blocks"][0]
    total = sum(segment["duration_sec"] for segment in block["segments"])
    assert abs(total - block["duration_sec"]) < 1.5


@pytest.mark.ffmpeg
def test_merge_writes_block_manifest(merged_block):
    """块清单必须落盘且可读回：它是模块边界的唯一事实源。"""
    ws, _first, _bytes = merged_block
    loaded = AudioMerger.load_manifest(ws)
    assert loaded and loaded["blocks"][0]["episodes"] == [11, 12]


@pytest.mark.ffmpeg
def test_merge_rerun_is_cached_and_manifest_untouched(merged_block):
    """重跑必须命中缓存且一个字节都不改块清单（改了就说明不幂等）。"""
    ws, _first, before = merged_block
    second = AudioMerger.merge(ws, [11, 12], target_minutes=1)
    assert second["status"] == "cached"
    assert AudioMerger.manifest_path(ws).read_bytes() == before


# ===========================================================================
# 11) fsutil 降级契约：不可访问的条目一律跳过，绝不抛异常
# ===========================================================================

def test_is_dir_returns_false_for_missing_path(tmp_path: Path):
    """`is_dir` 对不可访问路径返回 False，而不是把异常抛给调用方。"""
    assert fsutil.is_dir(tmp_path / "__definitely_missing__") is False


def test_file_size_returns_zero_for_missing_path(tmp_path: Path):
    """`file_size` 对不可访问文件返回 0（体积门槛据此当作未就绪跳过）。"""
    assert fsutil.file_size(tmp_path / "__definitely_missing__") == 0


def test_iter_child_dirs_yields_nothing_for_inaccessible_dir(tmp_path: Path):
    """不可访问目录产出空序列，让调用方走「无工作区」分支而不是整轮崩掉。"""
    assert list(fsutil.iter_child_dirs(tmp_path / "__definitely_missing__")) == []


def test_iter_files_yields_nothing_for_inaccessible_dir(tmp_path: Path):
    """不可访问目录产出空序列，单个坏链接不得让枚举中断。"""
    assert list(fsutil.iter_files(tmp_path / "__definitely_missing__", "*.md")) == []


def test_inaccessible_path_counts_as_reparse_point(tmp_path: Path):
    """不可访问的路径按「不可遍历」处理（返回 True），避免被当作普通目录跟随。"""
    assert fsutil.is_reparse_point(tmp_path / "__definitely_missing__") is True


def test_iter_child_dirs_skips_hidden_when_asked(tmp_path: Path):
    """`skip_hidden=True` 时只枚举非隐藏子目录。"""
    (tmp_path / "ws" / "articles").mkdir(parents=True)
    (tmp_path / ".hidden").mkdir()
    assert [path.name for path in fsutil.iter_child_dirs(tmp_path, skip_hidden=True)] == ["ws"]


def test_iter_child_dirs_lists_all_by_default(tmp_path: Path):
    """默认枚举全部子目录（含隐藏目录）。"""
    (tmp_path / "ws" / "articles").mkdir(parents=True)
    (tmp_path / ".hidden").mkdir()
    assert sorted(path.name for path in fsutil.iter_child_dirs(tmp_path)) == [".hidden", "ws"]


def test_iter_files_matches_pattern(tmp_path: Path):
    """`iter_files` 递归按 pattern 命中文件。"""
    articles = tmp_path / "ws" / "articles"
    write_text(articles / "P01_x_精读文章.md", "x" * 10)
    write_text(articles / "P01_x_TASK.md", "x" * 10)
    found = sorted(path.name for path in fsutil.iter_files(tmp_path, "*.md"))
    assert found == ["P01_x_TASK.md", "P01_x_精读文章.md"]


def _make_reparse_point(target: Path, link: Path) -> bool:
    """建一个指向 `target` 的重解析点；优先符号链接，Windows 无权限时退回 junction。

    实测事故就是手工建的 junction 让 5 个工作区变成 6 个，所以这里尽量用真实重解析点跑，
    而不是无权限就整条跳过。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
    )
    return proc.returncode == 0 and link.exists()


def test_iter_child_dirs_skips_reparse_point(tmp_path: Path):
    """符号链接 / junction 必须被跳过，否则同一工作区会被枚举两次。"""
    write_text(tmp_path / "ws" / "articles" / "a.md", "x")
    link = tmp_path / "link_to_ws"
    if not _make_reparse_point(tmp_path / "ws", link):
        pytest.skip("当前环境不允许创建符号链接/junction，跳过「链接被跳过」的实测")
    assert fsutil.is_reparse_point(link) is True
    assert link.name not in [path.name for path in fsutil.iter_child_dirs(tmp_path)]
