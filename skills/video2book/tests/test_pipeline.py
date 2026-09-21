# -*- coding: utf-8 -*-
"""流水线级契约：块级派发、回归修复项与「三路审查」硬化防线。

这里收的都是靠**整条链路**才能验出来的行为：

1. 转录任务书是转录角色唯一的输入——不得硬编码本机私有端点，也不得要求时间戳份数；
2. 分集拓扑缓存 `parts.json` 只能「只补不缩」：局部运行丢历史分集会让接口自愈误判课程规模；
3. 图文作品（抖音图集）必须被识别出来且不派发长文，否则写作环节只能编造；
4. 教材整编：抬头剥净、章内标题降级去号、章标题取长文 H1、册名取自内容、分册兜底与体量再切；
5. 畸形/过期块清单一律视为「无清单」，不许把崩溃点推到下游；
6. 磁盘上同名旧成品不得被当成本轮归并的缓存（否则归并篇永远派不出去）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from conftest import pad_to, write_blocks, write_text

from src.core.audio_merger import AudioMerger
from src.pipeline import (
    KIND_IMAGE_ALBUM,
    KIND_VIDEO,
    TRANSCRIBE_INSTRUCTION,
    export_block_transcribe_task,
    part_kind,
)
from src.core.ingestion.dyaudio.share_parser import parse_single_aweme
from src.core.task_cleanup import cleanup_completed_tasks, find_module_note
from src.core.workspace import TaskWorkspace, find_module_article, module_article_stem
from src.generator.block_synthesizer import BlockSynthesizer
from src.generator.integrator import ArticleIntegrator
from src.generator.topic_planner import SemanticTopicPlanner


# ---------------------------------------------------------------------------
# 就地构造辅助
# ---------------------------------------------------------------------------

# 一份「抬头含多行引用、章内带手写序号」的模块长文（整编的输入形态）
ARTICLE_WITH_HEADER = (
    "# 微型计算机概述\n"
    "> 目标：讲清体系结构  \n"
    "> 来源：模块长文\n"
    "\n---\n\n"
    "## 1. 体系结构\n\n正文内容。\n"
    + "填充正文，用于越过模块长文的 1000 字节成品门禁。" * 60
    + "\n"
)


def _integrate(ws: Any, blocks: Sequence[Dict[str, Any]], course_title: str = "测试课程",
               **kwargs: Any) -> List[Path]:
    """跑一次整编（每次都用新的 integrator，与 CLI 的行为一致）。"""
    return ArticleIntegrator(ws.root_dir).run(
        course_title=course_title, blocks=list(blocks), **kwargs
    )


def _volume_text(ws: Any, blocks: Sequence[Dict[str, Any]], index: int | None = None,
                 **kwargs: Any) -> str:
    """把整编结果拼成一段文本；给了 `index` 就只取那一册。"""
    books = _integrate(ws, blocks, course_title="探针课", **kwargs)
    if index is not None:
        books = books[index:index + 1]
    return "".join(book.read_text(encoding="utf-8") for book in books)


# ===========================================================================
# 1) 块级转录任务书：转录角色唯一的输入
# ===========================================================================

def test_block_transcribe_taskbook_lands_under_subtitles(make_workspace):
    """任务书按「块号_覆盖范围」落在 subtitles/：回收与逐字稿配对都靠这个名字。"""
    ws = make_workspace("转录任务书_BVTEST01")
    block = {"block_id": 1, "episodes": [1], "duration_min": 10.0}
    task_file = export_block_transcribe_task(ws, block)
    assert task_file == ws.subtitles_dir / "BLK01_P01_转录任务书.md"
    assert task_file.exists()


def test_block_transcribe_taskbook_embeds_transcription_instruction(make_workspace):
    """转录要求必须非空且原样写进任务书：它是转录角色唯一的指令来源。"""
    ws = make_workspace("转录任务书_BVTEST01")
    block = {"block_id": 1, "episodes": [1], "duration_min": 10.0}
    text = export_block_transcribe_task(ws, block).read_text(encoding="utf-8")
    assert TRANSCRIBE_INSTRUCTION.strip()
    assert TRANSCRIBE_INSTRUCTION in text


def test_block_transcribe_taskbook_has_no_private_endpoint(make_workspace):
    """任务书不得硬编码本机私有端点：换台机器照抄就整条链路失效。"""
    ws = make_workspace("转录任务书_BVTEST01")
    block = {"block_id": 1, "episodes": [1], "duration_min": 10.0}
    text = export_block_transcribe_task(ws, block).read_text(encoding="utf-8")
    assert "gemini-proxy-asr" not in text


def test_block_transcribe_taskbook_names_a_real_read_media_parameter(make_workspace):
    """任务书点名的参数必须是 `read_media` 真正暴露的那个。

    事故背景（2026-09）：任务书一直写 `instruction`，而 `read_media` 暴露的是 `prompt`
    （`instruction` 只是 `mode_prompt()` 的内部形参名）。转录方按字面传 `instruction`
    会被 schema 拒掉、或被静默丢弃 —— 那份「严禁摘要」的逐字要求根本没到模型，
    于是同一批块里有的正常转录、有的退化成摘要。
    """
    ws = make_workspace("转录任务书_BVTEST01")
    block = {"block_id": 1, "episodes": [1], "duration_min": 10.0}
    text = export_block_transcribe_task(ws, block).read_text(encoding="utf-8")
    assert "`prompt` = 第 2.1 节纯文本转录要求" in text
    assert "`instruction` = 第 2.1 节纯文本转录要求" not in text


def test_block_transcribe_taskbook_does_not_ask_for_timestamp_counts(make_workspace):
    """回报格式不得残留「时间戳份数」：转录按要求是纯文本，没有份数这回事。"""
    ws = make_workspace("转录任务书_BVTEST01")
    block = {"block_id": 1, "episodes": [1], "duration_min": 10.0}
    text = export_block_transcribe_task(ws, block).read_text(encoding="utf-8")
    assert "时间戳份数" not in text


# ===========================================================================
# 2) 分集拓扑缓存：局部运行不得丢历史分集
# ===========================================================================

def _legacy_parts() -> List[Dict[str, Any]]:
    return [{"page": 1, "title": "旧"}, {"page": 2, "title": "旧"}, {"page": 3, "title": "旧"}]


def test_merge_parts_keeps_history_and_order():
    """`--page N` 这类局部运行只带一集，合并后其余分集不得消失或乱序。"""
    merged = TaskWorkspace.merge_parts(_legacy_parts(), [{"page": 2, "title": "新"}])
    assert [item["page"] for item in merged] == [1, 2, 3]


def test_merge_parts_prefers_incoming_for_same_page():
    """同一个 page 以新结果为准（重新解析到的标题要生效）。"""
    merged = TaskWorkspace.merge_parts(_legacy_parts(), [{"page": 2, "title": "新"}])
    assert merged[1]["title"] == "新"


def test_merge_parts_accepts_empty_cache():
    """空缓存 + 增量 = 增量本身。"""
    assert TaskWorkspace.merge_parts([], [{"page": 5}]) == [{"page": 5}]


def test_merge_parts_accepts_empty_incoming():
    """空增量不得清空缓存（否则局部失败会把整个拓扑抹掉）。"""
    assert TaskWorkspace.merge_parts([{"page": 1}], []) == [{"page": 1}]


# ===========================================================================
# 3) 笔记复用：非规范后缀的成品也必须被认出
# ===========================================================================

@pytest.fixture
def note_probe(make_workspace):
    """一个工作区 + 一篇模块长文（笔记复用判定的语料）。"""
    ws = make_workspace("笔记复用_BVTEST01")
    ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
    article = write_text(ws.articles_dir / "模块01_绪论_精读长文.md", ARTICLE_WITH_HEADER)
    return ws, article


NOTE_META = {
    "block_id": 1, "block_title": "微机系统基础", "episodes": [1],
    "core_theme": "x", "blocks": [1],
}


def test_find_module_note_returns_none_when_absent(note_probe):
    """尚未有笔记成品时不得命中，否则笔记永远派发不出去。"""
    ws, _article = note_probe
    assert find_module_note(ws, 1) is None


def test_find_module_note_accepts_nonstandard_suffix(note_probe):
    """成品后缀不是规范名（无 `_笔记`）也要按编号前缀认出。"""
    ws, _article = note_probe
    loose = write_text(ws.notes_dir / "笔记01_微机系统基础_P01-P17速查.md", "笔记" * 600)
    assert find_module_note(ws, 1) == loose


def test_note_dispatch_skips_when_product_already_exists(note_probe):
    """已有笔记成品时不得重复派发（白烧一轮 token）。"""
    ws, article = note_probe
    write_text(ws.notes_dir / "笔记01_微机系统基础_P01-P17速查.md", "笔记" * 600)
    result = BlockSynthesizer.synthesize_block(dict(NOTE_META), [article], ws=ws)
    assert result["status"] == "cached"


def test_note_dispatch_writes_no_taskbook_when_cached(note_probe):
    """跳过派发时必须一份任务书都不落盘。"""
    ws, article = note_probe
    write_text(ws.notes_dir / "笔记01_微机系统基础_P01-P17速查.md", "笔记" * 600)
    BlockSynthesizer.synthesize_block(dict(NOTE_META), [article], ws=ws)
    assert not (ws.notes_dir / "笔记01_微机系统基础_TASK.md").exists()


# ===========================================================================
# 4) 教材整编：抬头剥净、标题降级、册名取自内容、复用与重建
# ===========================================================================

@pytest.fixture
def textbook_probe(make_workspace):
    """一个单块课程的工作区（长文抬头含多行引用，章内带手写序号）。"""
    ws = make_workspace("教材整编_BVTEST01")
    ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
    write_text(ws.articles_dir / "模块01_绪论_精读长文.md", ARTICLE_WITH_HEADER)
    block = {"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
             "duration_min": 30.0, "audio": "audio/_blocks/探针_01_绪论(P01).m4a"}
    return ws, block


def test_single_block_course_becomes_one_volume(textbook_probe):
    """一个块应编成一册。"""
    ws, block = textbook_probe
    assert len(_integrate(ws, [block])) == 1


def test_volume_name_comes_from_content(textbook_probe):
    """册名取自内容（这里无分节、标题无章节标记 → 用块标题兜底），不再是「第 N 册」。"""
    ws, block = textbook_probe
    assert _integrate(ws, [block])[0].name == "模块01_绪论_精读全书.md"


def test_article_h1_is_stripped_from_body(textbook_probe):
    """长文 H1 由章标题接管，正文里不得再出现一次篇名。"""
    ws, block = textbook_probe
    lines = [line.strip() for line in _integrate(ws, [block])[0].read_text(encoding="utf-8").splitlines()]
    assert "# 微型计算机概述" not in lines


def test_article_quote_header_is_stripped(textbook_probe):
    """多行引用抬头必须剥净：否则第二行 `>` 会残留成章首的孤立引用。"""
    ws, block = textbook_probe
    text = _integrate(ws, [block])[0].read_text(encoding="utf-8")
    assert "目标：讲清体系结构" not in text and "来源：模块长文" not in text


def test_article_h2_is_demoted_to_h3(textbook_probe):
    """章内 `##` 与教材章标题同级，必须降一级。"""
    ws, block = textbook_probe
    lines = _integrate(ws, [block])[0].read_text(encoding="utf-8").splitlines()
    assert "### 体系结构" in lines


def test_article_h2_no_longer_stays_at_h2(textbook_probe):
    """章内 `##` 不得以原层级残留（会与教材章标题并列成两个同层标题）。"""
    ws, block = textbook_probe
    lines = _integrate(ws, [block])[0].read_text(encoding="utf-8").splitlines()
    assert "## 体系结构" not in lines


def test_article_manual_numbering_is_stripped(textbook_probe):
    """整编要幂等地剥掉继承自长文的手写标题序号（阅读器会再自动编号一次）。"""
    ws, block = textbook_probe
    lines = _integrate(ws, [block])[0].read_text(encoding="utf-8").splitlines()
    assert "### 1. 体系结构" not in lines


def test_chapter_title_uses_article_h1(textbook_probe):
    """章标题优先用长文 H1（写作者提炼的主题），不是分集名。"""
    ws, block = textbook_probe
    lines = _integrate(ws, [block])[0].read_text(encoding="utf-8").splitlines()
    assert "## 微型计算机概述" in lines


def test_toc_lists_chapters_in_order(textbook_probe):
    """目录用有序列表，序号是目录序号，且带上覆盖范围。"""
    ws, block = textbook_probe
    lines = _integrate(ws, [block])[0].read_text(encoding="utf-8").splitlines()
    assert "1. 微型计算机概述（P01）" in lines


def test_episode_title_is_kept_in_block_trace(textbook_probe):
    """分集名退到「对应块」备注里保留，便于回溯这一章来自哪一讲。"""
    ws, block = textbook_probe
    text = _integrate(ws, [block])[0].read_text(encoding="utf-8")
    assert "块标题（分集名）：《绪论》" in text


def test_textbook_writes_no_manual_chapter_number(textbook_probe):
    """教材一律不写「第 N 章」：序号由阅读器生成。"""
    ws, block = textbook_probe
    text = _integrate(ws, [block])[0].read_text(encoding="utf-8")
    assert "第 1 章" not in text


def test_textbook_states_covered_span(textbook_probe):
    """册首必须写明本册覆盖的块范围。"""
    ws, block = textbook_probe
    text = _integrate(ws, [block])[0].read_text(encoding="utf-8")
    assert "覆盖范围**：P01 ~ P01" in text


def test_textbook_keeps_article_body(textbook_probe):
    """整编只补导读/目录/过渡，正文一字不得删。"""
    ws, block = textbook_probe
    text = _integrate(ws, [block])[0].read_text(encoding="utf-8")
    assert "正文内容。" in text


def test_existing_volume_is_reused_without_force(textbook_probe):
    """默认复用已存在的教材册（整编是纯工具动作，不必每次重算）。"""
    ws, block = textbook_probe
    out = _integrate(ws, [block])[0]
    out.write_text(out.read_text(encoding="utf-8") + "\n<!-- MARK -->\n", encoding="utf-8")
    _integrate(ws, [block])
    assert "<!-- MARK -->" in out.read_text(encoding="utf-8")


def test_force_rebuilds_existing_volume(textbook_probe):
    """`--force` 必须强制重新整编。"""
    ws, block = textbook_probe
    out = _integrate(ws, [block])[0]
    out.write_text(out.read_text(encoding="utf-8") + "\n<!-- MARK -->\n", encoding="utf-8")
    _integrate(ws, [block], force=True)
    assert "<!-- MARK -->" not in out.read_text(encoding="utf-8")


# ===========================================================================
# 5) 任务书回收：删除失败与成品未产出必须分开统计
# ===========================================================================

@pytest.fixture
def cleanup_probe(make_workspace):
    """两份模块长文任务书，其中只有 02 的成品已落盘。"""
    ws = make_workspace("任务书回收_BVTEST01")
    write_text(ws.articles_dir / "模块01_绪论_TASK.md", "t" * 200)
    write_text(ws.articles_dir / "模块02_数制_TASK.md", "t" * 200)
    write_text(ws.articles_dir / "模块02_数制_精读长文.md", "正文" * 400)
    return ws


def test_cleanup_keeps_one_sample_per_category(cleanup_probe):
    """每类保留编号最小的一份作提示词范本。"""
    result = cleanup_completed_tasks(cleanup_probe, keep_per_category=1)
    assert result["counts"]["articles"]["kept"] == 1


def test_cleanup_deletes_task_with_ready_product(cleanup_probe):
    """成品已落盘的任务书要回收，否则任务书从第一门课堆到第 N 门课。"""
    result = cleanup_completed_tasks(cleanup_probe, keep_per_category=1)
    assert result["counts"]["articles"]["deleted"] == 1


def test_cleanup_counts_pending_tasks_separately(cleanup_probe):
    """成品未产出的任务书计入 skipped_pending，不得混进 failed_delete。"""
    write_text(cleanup_probe.articles_dir / "模块03_体系_TASK.md", "t" * 200)
    result = cleanup_completed_tasks(cleanup_probe, keep_per_category=1)
    counts = result["counts"]["articles"]
    assert counts["skipped_pending"] == 1 and counts["failed_delete"] == 0


def test_cleanup_reports_no_failed_delete(cleanup_probe):
    """正常删除不应留下失败清单（留了就说明「删除失败」被说成了别的）。"""
    result = cleanup_completed_tasks(cleanup_probe, keep_per_category=1)
    assert result["counts"]["articles"]["failed_delete"] == 0
    assert list(result["failed_delete"]) == []


# ===========================================================================
# 6) 清单路径可移植性
# ===========================================================================

def test_saved_manifest_path_matches_to_relative(make_workspace):
    """教材路径落盘必须是相对形态，否则换机后清单里的绝对路径全部失效。"""
    ws = make_workspace("清单便携_BVTEST01")
    textbook_path = str(ws.root_dir / "textbooks" / "模块01_绪论_精读全书.md")
    ws.save_manifest({"textbooks": [textbook_path]})
    stored = json.loads(ws.manifest_file.read_text(encoding="utf-8"))["textbooks"][0]
    assert stored == TaskWorkspace.to_relative(textbook_path)


# ===========================================================================
# 7) 非视频作品（抖音图集）：识别与回读兜底
# ===========================================================================

def test_images_field_yields_image_album():
    """`images` 非空 → 图文作品：它没有口播，派发长文只会让写作环节编造。"""
    parsed = parse_single_aweme(
        {"aweme_id": "1", "images": [{"url_list": ["a"]}], "video": {"duration": 1000}}
    )
    assert parsed["media_kind"] == KIND_IMAGE_ALBUM


def test_plain_video_yields_video_kind():
    """普通视频不得被误判为图文作品。"""
    parsed = parse_single_aweme({"aweme_id": "2", "video": {"duration": 1000}})
    assert parsed["media_kind"] == KIND_VIDEO


def test_image_album_with_preview_video_still_image_album():
    """图文作品常带合成预览视频（play_addr 有值），用「有没有视频地址」反推会漏判。"""
    parsed = parse_single_aweme(
        {"aweme_id": "3", "images": [{"url_list": ["a"]}],
         "video": {"play_addr": {"url_list": ["https://v.example/x"]}}}
    )
    assert parsed["media_kind"] == KIND_IMAGE_ALBUM


def test_part_kind_falls_back_to_video_for_missing_or_blank():
    """缺字段 / null / 空串一律按视频处理：既有工作区不受新字段影响。"""
    assert part_kind({"page": 1}) == KIND_VIDEO
    assert part_kind({"page": 2, "media_kind": None}) == KIND_VIDEO
    assert part_kind({"page": 3, "media_kind": ""}) == KIND_VIDEO


def test_part_kind_respects_declared_kind():
    """显式声明了作品类型就按它走。"""
    assert part_kind({"page": 4, "media_kind": KIND_IMAGE_ALBUM}) == KIND_IMAGE_ALBUM


def test_merge_parts_still_overrides_title():
    """`media_kind` 兜底不得破坏其他字段的整体覆盖语义。"""
    merged = TaskWorkspace.merge_parts(
        [{"page": 1, "media_kind": KIND_IMAGE_ALBUM, "title": "图文"}],
        [{"page": 1, "title": "新标题"}],
    )
    assert len(merged) == 1 and merged[0]["title"] == "新标题"


def test_merge_parts_does_not_downgrade_media_kind():
    """incoming 不带该键时沿用旧值：否则已标记的图文作品会重新进入听音与派发。"""
    merged = TaskWorkspace.merge_parts(
        [{"page": 1, "media_kind": KIND_IMAGE_ALBUM, "title": "图文"}],
        [{"page": 1, "title": "新标题"}],
    )
    assert merged[0]["media_kind"] == KIND_IMAGE_ALBUM


def test_merge_parts_lets_incoming_correct_media_kind():
    """incoming 显式给出该键时以 incoming 为准（允许纠正误判）。"""
    merged = TaskWorkspace.merge_parts(
        [{"page": 1, "media_kind": KIND_IMAGE_ALBUM}],
        [{"page": 1, "media_kind": KIND_VIDEO, "title": "改判"}],
    )
    assert merged[0]["media_kind"] == KIND_VIDEO


def test_saved_parts_keep_media_kind(make_workspace):
    """分集拓扑写回 parts.json 后必须保留 media_kind，否则下次运行就认不出图文集。"""
    ws = make_workspace("图文落盘_BVTEST01")
    ws.save_parts(TaskWorkspace.merge_parts(
        ws.load_parts(), [{"page": 1, "title": "图文", "media_kind": KIND_IMAGE_ALBUM}]
    ))
    saved = ws.load_parts()
    assert saved and saved[0].get("media_kind") == KIND_IMAGE_ALBUM


# ===========================================================================
# 8) 畸形/过期块清单一律视为「无清单」
# ===========================================================================

BAD_MANIFESTS = [
    '{"version": 2, "blocks": [{"block_id": 1}]}',          # 条目缺 episodes
    '{"version": 2, "blocks": [null]}',                     # 条目不是对象
    '{"version": 2, "blocks": [{"block_id": 0, "episodes": [1]}]}',   # 块号非法
    '{"version": 1, "blocks": [{"block_id": 1, "episodes": [1]}]}',   # 旧版本
    '{"version": 2, "blocks": []}',                          # 空表
]


@pytest.mark.parametrize("payload", BAD_MANIFESTS)
def test_malformed_manifest_is_treated_as_absent(make_workspace, payload: str):
    """照单全收的后果实测过：`block_stem` 抛 IndexError，装箱/队列/对账/整编一路崩穿。"""
    ws = make_workspace("畸形清单_BVTEST01")
    write_text(ws.audio_dir / "_blocks" / "blocks.json", payload)
    assert AudioMerger.load_manifest(ws) is None


def test_malformed_manifest_yields_no_blocks_for_planner(make_workspace):
    """畸形清单不得进入笔记归并链路。"""
    ws = make_workspace("畸形清单_BVTEST01")
    write_text(ws.audio_dir / "_blocks" / "blocks.json", BAD_MANIFESTS[0])
    assert SemanticTopicPlanner.load_blocks(ws) == []


def test_malformed_manifest_yields_no_blocks_for_integrator(make_workspace):
    """畸形清单不得进入教材整编链路。"""
    ws = make_workspace("畸形清单_BVTEST01")
    write_text(ws.audio_dir / "_blocks" / "blocks.json", BAD_MANIFESTS[0])
    assert ArticleIntegrator(ws.root_dir).load_blocks() == []


# ===========================================================================
# 9) 整编的纯路径入参、缺长文 gate、块号 >99
# ===========================================================================

PATH_PROBE_BLOCK = {"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
                    "duration_min": 46.0, "audio": "audio/_blocks/探针_01_绪论(P01).m4a"}


def test_integrator_reads_blocks_from_plain_path(make_workspace):
    """`ArticleIntegrator` 收的是纯 Path：块清单必须能按路径读到（否则整编永远空跑）。"""
    ws = make_workspace("路径入参_BVTEST01")
    write_blocks(ws, [dict(PATH_PROBE_BLOCK)])
    assert ArticleIntegrator(ws.root_dir).load_blocks()


def test_integrator_without_articles_returns_no_volume(make_workspace):
    """缺长文的块 gate 跳过，本轮不产任何册。"""
    ws = make_workspace("路径入参_BVTEST01")
    write_blocks(ws, [dict(PATH_PROBE_BLOCK)])
    assert ArticleIntegrator(ws.root_dir).run(course_title="探针课", blocks=None) == []


def test_integrator_without_articles_writes_no_placeholder(make_workspace):
    """缺长文时不得落占位教材——占位一旦被缓存就会永久留存。"""
    ws = make_workspace("路径入参_BVTEST01")
    write_blocks(ws, [dict(PATH_PROBE_BLOCK)])
    ArticleIntegrator(ws.root_dir).run(course_title="探针课", blocks=None)
    assert not list((ws.root_dir / "textbooks").glob("*.md"))


def test_module_article_stem_supports_block_id_over_99():
    """块号 >99 时不得退化成两位编号（写与读必须是同一口径）。"""
    big = {"block_id": 100, "title": "越界块", "span": "P100-P101", "episodes": [100]}
    assert module_article_stem(big).startswith("模块100_")


def test_find_module_article_supports_block_id_over_99(make_workspace):
    """块号 >99 的长文必须能被找回，否则大课的第 100 块永远整编不进去。"""
    ws = make_workspace("越界块号_BVTEST01")
    big = {"block_id": 100, "title": "越界块", "span": "P100-P101", "episodes": [100]}
    target = write_text(ws.articles_dir / f"{module_article_stem(big)}_精读长文.md", "正文" * 400)
    assert find_module_article(ws.articles_dir, big) == target


# ===========================================================================
# 10) 归并抢救：重复认领先到先得、未知块忽略、孤块兜底
# ===========================================================================

SALVAGE_BLOCKS = [
    {"block_id": 1, "title": "A", "episodes": [1]},
    {"block_id": 2, "title": "B", "episodes": [2]},
    {"block_id": 3, "title": "C", "episodes": [3]},
]


def _salvage() -> List[Dict[str, Any]]:
    salvaged, _diag = SemanticTopicPlanner.salvage_notes(
        [{"note_id": 1, "note_title": "X", "blocks": [1, 2, 2, 99]}], SALVAGE_BLOCKS
    )
    return salvaged


def test_salvage_notes_dedupes_and_bootstraps_orphans():
    """重复认领只算一次、未知块丢掉、没人认领的块各自补一篇兜底（内容不能凭空消失）。"""
    assert [note["blocks"] for note in _salvage()] == [[1, 2], [3]]


def test_salvage_notes_derives_episodes_for_orphan():
    """兜底笔记的集号要从它认领的块推导出来，否则派发出去的笔记没有覆盖范围。"""
    assert [note["episodes"] for note in _salvage()] == [[1, 2], [3]]


# ===========================================================================
# 11) 归并态下的同编号旧成品遮蔽
# ===========================================================================

@pytest.fixture
def merged_note_probe(make_workspace):
    """已进入归并态的工作区：盘上有 note_plan.json，以及一块一篇时代的旧成品。"""
    ws = make_workspace("遮蔽探针_BVTEST01")
    ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
    write_blocks(ws, [
        {"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
         "duration_min": 46.0, "audio": "audio/a.m4a"},
        {"block_id": 2, "title": "数制", "span": "P02", "episodes": [2],
         "duration_min": 46.0, "audio": "audio/b.m4a"},
    ])
    for block_id, title in ((1, "绪论"), (2, "数制")):
        write_text(ws.articles_dir / f"模块{block_id:02d}_{title}_精读长文.md", "正文" * 400)
    write_text(ws.root_dir / "note_plan.json", json.dumps(
        [{"note_id": 1, "note_title": "归并篇", "blocks": [1, 2], "core_theme": "x"}],
        ensure_ascii=False,
    ))
    write_text(ws.notes_dir / "笔记01_旧粒度主题_笔记.md", "旧成品" * 400)
    return ws


def test_merged_plan_ignores_same_id_legacy_product(merged_note_probe):
    """宽容前缀会把「同编号的旧粒度成品」当成新归并笔记的缓存，导致归并篇永远派不出去。"""
    result = BlockSynthesizer.dispatch_notes(
        merged_note_probe, [{"page": 1}, {"page": 2}], course_title="探针课"
    )
    task_names = [Path(item["task_file"]).name for item in result["results"]]
    assert task_names == ["笔记01_归并篇_TASK.md"]


# ===========================================================================
# 12) 教材分册：内容优先（Agent 规划 → 平台分节），体量兜底
# ===========================================================================

VOLUME_BLOCKS = [
    {"block_id": 1, "title": "甲", "span": "P01", "episodes": [1],
     "duration_min": 46.0, "audio": "audio/a.m4a"},
    {"block_id": 2, "title": "乙", "span": "P02", "episodes": [2],
     "duration_min": 46.0, "audio": "audio/b.m4a"},
    {"block_id": 3, "title": "丙", "span": "P03", "episodes": [3],
     "duration_min": 46.0, "audio": "audio/c.m4a"},
]


def _write_volume_articles(ws: Any, blocks: Sequence[Dict[str, Any]], h1: bool = True) -> None:
    """每个块一篇长文；`h1=False` 时长文不写主题（章标题退化为块标题）。"""
    for block in blocks:
        title = str(block["title"])
        head = f"# {title} 长文\n\n" if h1 else ""
        write_text(
            ws.articles_dir / f"模块{int(block['block_id']):02d}_{title}_精读长文.md",
            head + "## 小节\n\n" + ("正文内容。" * 300) + "\n",
        )


@pytest.fixture
def volume_probe(make_workspace):
    """三个块的长文 + 带平台分节的 parts.json（无 Agent 分册规划）。"""
    ws = make_workspace("分册探针_BVTEST01")
    write_blocks(ws, [dict(block) for block in VOLUME_BLOCKS])
    _write_volume_articles(ws, VOLUME_BLOCKS)
    write_text(ws.root_dir / "parts.json", json.dumps([
        {"page": 1, "title": "甲", "section_title": "第一章 甲"},
        {"page": 2, "title": "乙", "section_title": "第一章 甲"},
        {"page": 3, "title": "丙", "section_title": "第二章 丙"},
    ], ensure_ascii=False))
    return ws


def _write_plan(ws: Any, volumes: Sequence[Dict[str, Any]]) -> None:
    write_text(ws.root_dir / "textbook_plan.json", json.dumps(list(volumes), ensure_ascii=False))


def test_platform_sections_split_volumes_by_section(volume_probe):
    """无规划但有平台分节时按分节成册，册名就是节名（册名本身要能看出内容）。"""
    books = _integrate(volume_probe, VOLUME_BLOCKS, course_title="探针课")
    assert [book.name for book in books] == [
        "模块01_第一章 甲_精读全书.md",
        "模块02_第二章 丙_精读全书.md",
    ]


def test_volume_header_states_split_basis(volume_probe):
    """多册时册首必须写明分册依据。"""
    books = _integrate(volume_probe, VOLUME_BLOCKS, course_title="探针课")
    assert "全书按内容分 2 册" in books[0].read_text(encoding="utf-8")


def test_missing_plan_exports_planning_task(volume_probe):
    """缺归并/分册规划时不阻塞流程，但要导出规划任务书让 Agent 补齐。"""
    _integrate(volume_probe, VOLUME_BLOCKS, course_title="探针课")
    assert (volume_probe.root_dir / "textbook_plan_TASK.md").exists()


def test_agent_plan_takes_priority_over_platform_sections(volume_probe):
    """Agent 规划优先于平台分节：册名来自规划。"""
    _write_plan(volume_probe, [
        {"volume_id": 1, "volume_title": "甲与乙：基础篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "丙：进阶篇", "blocks": [3]},
    ])
    books = _integrate(volume_probe, VOLUME_BLOCKS, course_title="探针课", force=True)
    assert [book.name for book in books] == [
        "模块01_甲与乙：基础篇_精读全书.md",
        "模块02_丙：进阶篇_精读全书.md",
    ]


def test_plan_volume_assigns_chapters_by_block(volume_probe):
    """规划分册的章分配必须严格按它认领的块。"""
    _write_plan(volume_probe, [
        {"volume_id": 1, "volume_title": "甲与乙：基础篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "丙：进阶篇", "blocks": [3]},
    ])
    text = _volume_text(volume_probe, VOLUME_BLOCKS, index=0, force=True)
    assert "## 甲" in text and "## 乙" in text and "## 丙" not in text


def test_volume_header_uses_plan_title(volume_probe):
    """册名要写进册首信息，读者一眼知道这册讲什么。"""
    _write_plan(volume_probe, [
        {"volume_id": 1, "volume_title": "甲与乙：基础篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "丙：进阶篇", "blocks": [3]},
    ])
    text = _volume_text(volume_probe, VOLUME_BLOCKS, force=True)
    assert "本册内容**：甲与乙：基础篇" in text


def _split_volumes(ws: Any) -> List[Path]:
    """把三个块规划成一册，再用很小的体量上限逼出「按块边界再切」。"""
    _write_plan(ws, [{"volume_id": 1, "volume_title": "甲乙丙合集", "blocks": [1, 2, 3]}])
    return _integrate(ws, VOLUME_BLOCKS, course_title="探针课", force=True, size_cap_bytes=5000)


def test_oversized_volume_splits_at_block_boundaries(volume_probe):
    """单册超上限时在块边界再切，绝不在块中间断开。"""
    assert len(_split_volumes(volume_probe)) == 3


def test_split_volume_labels_use_part_suffix(volume_probe):
    """体量再切的三册用上/中/下标注，读者才知道顺序。"""
    books = _split_volumes(volume_probe)
    assert "（上）" in books[0].name and "（下）" in books[-1].name


def test_split_volumes_keep_chapter_order(volume_probe):
    """跨册章序不得乱：切册是机械动作，不该重排内容。"""
    books = _split_volumes(volume_probe)
    joined = "".join(book.read_text(encoding="utf-8") for book in books)
    assert joined.index("## 甲") < joined.index("## 乙") < joined.index("## 丙")


def test_stale_volumes_are_removed(volume_probe):
    """教材是纯派生数据：本轮不再产出的旧册必须清掉。"""
    stale = write_text(volume_probe.root_dir / "textbooks" / "模块99_旧册_精读全书.md", pad_to("旧教材"))
    books = _split_volumes(volume_probe)
    left = sorted(path.name for path in (volume_probe.root_dir / "textbooks").glob("*_精读全书.md"))
    assert left == sorted(book.name for book in books)
    assert stale.name not in left


def test_block_without_article_is_not_written_into_textbook(volume_probe):
    """缺模块长文的块 gate 跳过，不得进书（旧实现会写 ⚠️ 占位章）。"""
    (volume_probe.articles_dir / "模块03_丙_精读长文.md").unlink()
    text = _volume_text(volume_probe, VOLUME_BLOCKS)
    assert "## 丙" not in text


def test_block_without_article_leaves_no_placeholder(volume_probe):
    """缺长文的块不得落占位内容（占位会被缓存永久留存）。"""
    (volume_probe.articles_dir / "模块03_丙_精读长文.md").unlink()
    text = _volume_text(volume_probe, VOLUME_BLOCKS)
    assert "⚠️" not in text


# ---------------------------------------------------------------------------
# 12b) 同名章消歧（劈分腿）、章标题取长文 H1
# ---------------------------------------------------------------------------

DUP_BLOCKS = [
    {"block_id": 1, "title": "关系数据库（下）", "span": "P06上", "episodes": [6],
     "duration_min": 43.6, "audio": "audio/d1.m4a"},
    {"block_id": 2, "title": "关系数据库（下）", "span": "P06下", "episodes": [6],
     "duration_min": 43.6, "audio": "audio/d2.m4a"},
]


@pytest.fixture
def duplicate_chapter_probe(make_workspace):
    """两章同名（同一集被劈成上下两条腿，块标题一样）。"""
    ws = make_workspace("同名章探针_BVTEST01")
    write_blocks(ws, [dict(block) for block in DUP_BLOCKS])
    for block_id in (1, 2):
        write_text(ws.articles_dir / f"模块{block_id:02d}_关系数据库（下）_精读长文.md",
                   "正文内容。" * 300)
    return ws


def _duplicate_chapter_text(ws: Any) -> str:
    return _volume_text(ws, DUP_BLOCKS, force=True)


def test_duplicate_chapter_titles_get_span_suffix(duplicate_chapter_probe):
    """同名章必须按覆盖范围消歧，否则书里出现两个同名章。"""
    text = _duplicate_chapter_text(duplicate_chapter_probe)
    assert "## 关系数据库（下）（P06上）" in text
    assert "## 关系数据库（下）（P06下）" in text


def test_transition_line_uses_disambiguated_titles(duplicate_chapter_probe):
    """过渡句不得变成「上一节讲完 A，下一节接着讲 A」。"""
    text = _duplicate_chapter_text(duplicate_chapter_probe)
    assert "上一节讲完「关系数据库（下）（P06上）」，下一节接着讲「关系数据库（下）（P06下）」" in text


def test_episode_count_dedupes_split_legs(duplicate_chapter_probe):
    """讲数按去重集号统计：劈分腿不能让同一集被数两遍。"""
    text = _duplicate_chapter_text(duplicate_chapter_probe)
    assert "共 1 讲" in text


def test_toc_does_not_repeat_span_suffix(duplicate_chapter_probe):
    """目录不得把消歧范围又补一遍（会成「（P06上）（P06上）」）。"""
    text = _duplicate_chapter_text(duplicate_chapter_probe)
    assert "（P06上）（P06上）" not in text and "（P06下）（P06下）" not in text


def test_toc_lists_disambiguated_chapter(duplicate_chapter_probe):
    """目录也要用消歧后的章标题。"""
    text = _duplicate_chapter_text(duplicate_chapter_probe)
    assert "1. 关系数据库（下）（P06上）" in text


H1_BLOCK = {"block_id": 1, "title": "数据库第2章 关系数据库 （上）", "span": "P05",
            "episodes": [5], "duration_min": 57.0, "audio": "audio/h1.m4a"}


@pytest.fixture
def h1_probe(make_workspace):
    """块标题是带平台编号的分集名，长文自己写了真正的主题 H1。"""
    ws = make_workspace("长文主题探针_BVTEST01")
    write_blocks(ws, [dict(H1_BLOCK)])
    write_text(
        ws.articles_dir / "模块01_数据库第2章 关系数据库 （上）_精读长文.md",
        "# 关系模型结构与数据完整性约束\n\n## 关系数据结构\n\n" + ("正文内容。" * 300) + "\n",
    )
    return ws


def _h1_text(ws: Any) -> str:
    return _volume_text(ws, [dict(H1_BLOCK)], force=True)


def test_chapter_title_prefers_article_h1(h1_probe):
    """章标题优先长文 H1（写作者提炼的主题），不照抄带平台编号的分集名。"""
    assert "## 关系模型结构与数据完整性约束" in _h1_text(h1_probe)


def test_toc_uses_article_h1(h1_probe):
    """目录同样用长文 H1。"""
    assert "1. 关系模型结构与数据完整性约束（P05）" in _h1_text(h1_probe)


def test_episode_name_is_kept_in_block_trace(h1_probe):
    """分集名退到「对应块」备注里保留，便于回溯。"""
    assert "块标题（分集名）：《数据库第2章 关系数据库 （上）》" in _h1_text(h1_probe)


# ===========================================================================
# 13) 接地门禁（`check --stage1`）：跑得通、退出 0、如实报告未就绪
# ===========================================================================

GROUNDING_BLOCK = {"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
                   "duration_min": 46.0, "audio": "audio/a.m4a", "segments": []}


@pytest.fixture
def grounding_ws(make_workspace):
    """有块清单、但一块模块长文都没有的工作区。"""
    ws = make_workspace("接地探针_BVTEST01")
    write_blocks(ws, [dict(GROUNDING_BLOCK)])
    return ws


def test_stage1_check_exits_zero_without_articles(grounding_ws, run_cli):
    """默认是提示级：尚无长文也要 exit 0（加 --strict 才纳入门禁）。"""
    result = run_cli("check", "--stage1", "--dir", str(grounding_ws.root_dir))
    assert result.code == 0, result.out[-300:]


def test_stage1_check_reports_missing_module_articles(grounding_ws, run_cli):
    """必须如实报出「尚无模块长文」，否则 Agent 会以为阶段一已经就绪。"""
    result = run_cli("check", "--stage1", "--dir", str(grounding_ws.root_dir))
    assert "无模块长文" in result.out
