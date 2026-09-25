# -*- coding: utf-8 -*-
"""笔记侧行为契约：块即模块 / 归并按块号校验 / 笔记任务书与回收。

迁移自 `scripts/selfcheck.py` 的三段行为断言：
`check_note_planner_contract`（430-480）、`check_module_note_contract`（1634-1762）、
`check_block_note_merge_contract`（2722-2852）。

每条测试对应一类实测过的故障，docstring 写清「防的是哪种真实故障」；裸的形态断言
（「某个私有函数不许回来」「源码里不许出现某串」）不在此处，见迁移报告。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import pad_to, write_blocks, write_text

from src.core.task_cleanup import cleanup_completed_tasks, find_module_note
from src.generator.block_synthesizer import BlockSynthesizer
from src.prompts import MODULE_NOTE_PROMPT, NOTE_VISUAL_SPEC
from src.generator.topic_planner import SemanticTopicPlanner as P

# `--range 9-87` 这类工作区的实际集号：集号基准是它，不是序号 1..79
PAGES = list(range(9, 88))


def _two_blocks():
    """两个块恰好覆盖 PAGES（前 40 集 / 后 39 集）。"""
    return [
        {"block_id": 1, "title": "A", "episodes": PAGES[:40]},
        {"block_id": 2, "title": "B", "episodes": PAGES[40:]},
    ]


# ---------------------------------------------------------------------------
# 集号基准与描述（工作区集号是事实，工具无权改成 1..N）
# ---------------------------------------------------------------------------

def test_describe_pages_keeps_actual_episode_numbers():
    """集号基准用实际集号：要求 `--range 9-87` 的工作区重排成 1..79 会让它阶段二永久不可达。"""
    assert P.describe_pages(PAGES) == "P09–P87"


def test_describe_episodes_single_episode():
    assert P.describe_episodes([9]) == "P09"


def test_describe_episodes_contiguous_range():
    assert P.describe_episodes([9, 10]) == "P09–P10"


def test_describe_blocks_sorts_and_prefixes():
    """任务书里「涵盖块」要写清块号，乱序会让人误以为笔记跨的是别的块。"""
    assert P.describe_blocks([3, 1]) == "块 01、块 03"


# ---------------------------------------------------------------------------
# 归并校验：块恰好被认领一次 + 集号全覆盖
# ---------------------------------------------------------------------------

def test_validate_note_plan_accepts_cross_block_note():
    """一篇笔记装多个块是常态：块各认领一次、集号推导齐全即合法。"""
    ok, _ = P.validate_note_plan(
        [{"note_id": 1, "note_title": "跨块笔记", "blocks": [1, 2]}], _two_blocks(), PAGES
    )
    assert ok


def test_note_episodes_derives_all_episodes_of_claimed_blocks():
    """集号由 blocks 推导（note 里自填的 episodes 不作数），跨块笔记必须拿到全部集号。"""
    assert P.note_episodes({"blocks": [1, 2]}, _two_blocks()) == PAGES


@pytest.mark.parametrize(
    "note_plan",
    [
        pytest.param([{"note_id": 1, "note_title": "x", "blocks": [1]}], id="漏掉块-2"),
        pytest.param([{"note_id": 1, "note_title": "x", "blocks": [1, 2, 2]}], id="块-2-被认领两次"),
        pytest.param([{"note_id": 1, "note_title": "x", "blocks": [1, 3]}], id="引用不存在的块"),
        pytest.param([{"note_id": 1, "note_title": "x", "blocks": []}], id="未认领任何块"),
    ],
)
def test_validate_note_plan_rejects_incomplete_or_duplicated_claims(note_plan):
    """漏块 = 内容凭空消失；重复认领 = 同一批内容写两遍。两种都必须被拒。"""
    assert not P.validate_note_plan(note_plan, _two_blocks(), PAGES)[0]


# ---------------------------------------------------------------------------
# 长文标题：归并的判断依据
# ---------------------------------------------------------------------------

def test_read_article_title_prefers_h1(tmp_path):
    """H1 是写作者读完语料提炼的主题，归并必须按它判亲缘，而不是分集标题。"""
    art = write_text(
        tmp_path / "模块03_核心语法与函数_精读长文.md",
        "# 变量、作用域与函数\n\n正文…\n",
    )
    assert P.read_article_title(art) == "变量、作用域与函数"


def test_read_article_title_falls_back_to_filename(tmp_path):
    """无 H1 的长文用文件名兜底：`模块XX_` 前缀与 `_精读长文` 后缀都要剥掉。"""
    art = write_text(
        tmp_path / "模块03_核心语法与函数_精读长文.md", "没有 H1 的长文\n"
    )
    assert P.read_article_title(art) == "核心语法与函数"


# ---------------------------------------------------------------------------
# 模块笔记提示词：套话 / 分集口吻 / 截断都得点名禁止
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase, 说明",
    [
        ("概念属性与边界", "套话黑名单"),
        ("严禁用分集编号或分集标题作标题", "分集标题禁令"),
        ("上一讲", "分集口吻禁令"),
        ("不中途截断", "截断禁令"),
    ],
)
def test_module_note_prompt_bans_filler_and_episode_tone(phrase, 说明):
    """少了这句，模型会写套话、按分集编号分节、用「上一讲」口吻、写到一半截断。"""
    assert phrase in MODULE_NOTE_PROMPT, f"MODULE_NOTE_PROMPT 缺少{说明}：{phrase}"


# ---------------------------------------------------------------------------
# 【排版】规范
# ---------------------------------------------------------------------------

def test_note_visual_spec_restricts_shape_to_h1_and_sections():
    """成品只有 H1 + 按知识主题分节的条目；多写目录/拓扑树/速查卡会与标题重复。"""
    assert "不写目录、元信息引用块、知识拓扑树、节级主旨句、速查卡、总纲" in NOTE_VISUAL_SPEC


def test_note_visual_spec_drops_source_annotation_sample():
    """来源标注（`来源: P03`）在成品里不许出现，样板留着就会被照抄。"""
    assert "来源: P03" not in NOTE_VISUAL_SPEC


def test_note_visual_spec_requires_fenced_ascii_art():
    """字符画裸写在正文里会被渲染器合并空格、彻底错位。"""
    assert "```text" in NOTE_VISUAL_SPEC


def test_note_visual_spec_requires_fence_indent_inside_list():
    """围栏写在列表项里顶格会截断列表，必须整体缩进 4 空格。"""
    assert "字符画写在列表项里时，围栏整体缩进 4 空格" in NOTE_VISUAL_SPEC


# ---------------------------------------------------------------------------
# 标题纪律：层级上限、不写序号、不设数字配额
# ---------------------------------------------------------------------------

def test_prompt_caps_heading_depth_at_h4():
    assert "最多到 `####`" in MODULE_NOTE_PROMPT


def test_prompt_forbids_manual_heading_numbers():
    """阅读器会给标题自动编号，手写序号会叠成 `1.1.` 那种乱码。"""
    assert "标题里一律不要写序号" in MODULE_NOTE_PROMPT


@pytest.mark.parametrize("数字配额", ["8~12 节", "20~45 个", "2~3 条要点"])
def test_prompt_has_no_numeric_quota(数字配额):
    """写死配额会诱导模型机械凑数或粗暴削内容，反而失真。"""
    assert 数字配额 not in MODULE_NOTE_PROMPT


def test_prompt_does_not_revive_legacy_h3_ban():
    """旧版「不得出现 ###」与现行「最多到 ####」正面打架，回流即双重标准。"""
    assert "不得出现 `###` 及更深的标题" not in MODULE_NOTE_PROMPT


def test_prompt_requires_nested_bullet_skeleton():
    assert "逐级 4 空格缩进的 `*` 列表" in MODULE_NOTE_PROMPT


def test_prompt_requires_conclusions_only():
    """只写结论：带推导过程的笔记会把速查笔记写成第二篇讲义。"""
    assert "只写结论" in MODULE_NOTE_PROMPT


def test_prompt_requires_plain_term_headings():
    assert "标题只写术语或名词短语" in MODULE_NOTE_PROMPT


def test_prompt_requires_two_items_per_section():
    """`###` 只剩一两条说明拆碎了，应并回上一节。"""
    assert "每个 `###` 至少两条条目" in MODULE_NOTE_PROMPT


def test_prompt_requires_code_fence():
    assert "```text" in MODULE_NOTE_PROMPT


def test_prompt_forbids_external_knowledge():
    """笔记只许来自长文；补课程外知识会让读者无法回到原文核对。"""
    assert "禁止引入外部知识" in MODULE_NOTE_PROMPT


def test_prompt_requires_gap_marker():
    """长文没讲的关键字段写「长文未说明」，而不是靠脑补填满。"""
    assert "长文未说明" in MODULE_NOTE_PROMPT


@pytest.mark.parametrize(
    "已取消", ["NOTE_SUPPLEMENT_RULES", "六、补充规范", "严格限量，宁缺勿补"]
)
def test_prompt_drops_retired_supplement_rules(已取消):
    """补充规范已随换版取消，提示词里残留旧章节会与「禁止外部知识」自相矛盾。"""
    assert 已取消 not in MODULE_NOTE_PROMPT


# ---------------------------------------------------------------------------
# 结构门禁与提示词同步（可复算）
# ---------------------------------------------------------------------------

def test_note_structure_gate_accepts_compliant_note():
    """合规笔记（H1 + 分节 + 最多 #### + 不写序号）不得被门禁误判。"""
    from src.core.deliverable_lint import check_note_structure

    合规 = check_note_structure(
        "# 标题\n\n## 主题一\n\n* **术语**\n\n    * 条目一。\n\n### 子题\n\n    * 条目二。\n\n"
        "#### 更深一层\n\n    * 条目三。\n\n## 主题二\n\n    * 条目四。\n"
    )
    assert 合规["no_h5plus_headings"] and 合规["no_numbered_headings"], 合规


def test_note_structure_gate_flags_manual_numbers():
    """带手写序号的标题必须被检出，否则会与阅读器编号叠字交付出去。"""
    from src.core.deliverable_lint import check_note_structure

    违规 = check_note_structure("# 标题\n\n## 1. 手写序号\n\n#### 可以\n\n##### 太深\n")
    assert not 违规["no_numbered_headings"]


def test_note_structure_gate_flags_h5_and_deeper():
    """笔记标题上限是 `####`：`#####` 必须被检出（层级过深会碎成一地小节）。"""
    from src.core.deliverable_lint import check_note_structure

    违规 = check_note_structure("# 标题\n\n## 1. 手写序号\n\n#### 可以\n\n##### 太深\n")
    assert not 违规["no_h5plus_headings"]


# ---------------------------------------------------------------------------
# 笔记任务书渲染：长文直供 + 【排版】注入
# ---------------------------------------------------------------------------

def _rendered_note_prompt(skill_root: Path) -> str:
    """用一份真实存在的文件当语料，渲染一篇笔记的任务书提示词。"""
    block_meta = {
        "block_id": 3, "block_title": "关系数据库", "episodes": [6, 7], "core_theme": "关系模型",
    }
    return BlockSynthesizer.build_synthesis_prompt(block_meta, [skill_root / "SKILL.md"])


def test_task_prompt_injects_visual_spec(skill_root):
    """【排版】一节必须进任务书：它就是笔记版式的唯一文案来源。"""
    prompt = _rendered_note_prompt(skill_root)
    assert "【排版】" in prompt
    assert "字符画写在列表项里时，围栏整体缩进 4 空格" in prompt


def test_task_prompt_substitutes_block_title_into_h1(skill_root):
    """H1 占位符没被替换，子智能体就会照着一个空标题去写。"""
    prompt = _rendered_note_prompt(skill_root)
    assert "# 关系数据库" in prompt


def test_task_prompt_lists_module_articles_as_only_corpus(skill_root):
    """语料必须写明是**模块长文**并给出绝对路径与字节数，否则子智能体会去翻逐字稿。"""
    prompt = _rendered_note_prompt(skill_root)
    assert "模块长文" in prompt
    assert "SKILL.md" in prompt and "字节" in prompt


def test_task_prompt_has_no_removed_knowledge_index(skill_root):
    """知识元索引已随逐集链路移除，回加会让任务书指向不存在的东西。"""
    prompt = _rendered_note_prompt(skill_root)
    assert "可选结构化索引" not in prompt


# ---------------------------------------------------------------------------
# 任务书回收：成品已产出才回收，每类保留 1 份范本
# ---------------------------------------------------------------------------

def _seed_cleanup_workspace(make_workspace):
    """造一个三分类都含「成品已产出 / 未产出 / 空壳」的工作区。"""
    ws = make_workspace("cleanup_task")

    # 模块长文：模块01 无成品（保留待办 + 范本）、模块02 有成品（回收）
    write_text(ws.articles_dir / "模块01_绪论_TASK.md", "t" * 200)
    write_text(ws.articles_dir / "模块02_数制_TASK.md", "t" * 200)
    write_text(ws.articles_dir / "模块02_数制_精读长文.md", pad_to("内容"))

    # 笔记任务书：笔记01 有成品（范本，保留）、笔记02 有成品（回收）、笔记03 无成品（保留）
    for name in ("笔记01_绪论_TASK.md", "笔记02_关系_TASK.md", "笔记03_理论_TASK.md"):
        write_text(ws.notes_dir / name, "t" * 200)
    write_text(ws.notes_dir / "笔记01_绪论_笔记.md", pad_to("笔记"))
    write_text(ws.notes_dir / "笔记02_关系_笔记.md", pad_to("笔记"))

    # 块级转录任务书：BLK01 有成品（范本，保留）、BLK02 有成品（回收）、
    # BLK03 的成品是空文件（不算成品，保留待办）
    for name in ("BLK01_P01_转录任务书.md", "BLK02_P02_转录任务书.md", "BLK03_P03_转录任务书.md"):
        write_text(ws.subtitles_dir / name, "t" * 200)
    write_text(ws.subtitles_dir / "BLK02_P02_逐字稿.md", pad_to("逐字稿"))
    write_text(ws.subtitles_dir / "BLK03_P03_逐字稿.md", "")
    return ws


def test_cleanup_reclaims_article_task_with_product(make_workspace):
    """模块长文已落盘 → 任务书是废纸，回收；未落盘的那份必须留着待办。"""
    ws = _seed_cleanup_workspace(make_workspace)
    cleanup_completed_tasks(ws, keep_per_category=1, dry_run=False)
    assert sorted(p.name for p in ws.articles_dir.glob("*_TASK.md")) == ["模块01_绪论_TASK.md"]


def test_cleanup_keeps_note_sample_and_pending_tasks(make_workspace):
    """笔记：编号最小的那份留作提示词范本，成品未产出的任务书一律保留。"""
    ws = _seed_cleanup_workspace(make_workspace)
    cleanup_completed_tasks(ws, keep_per_category=1, dry_run=False)
    remaining = sorted(p.name for p in ws.notes_dir.glob("*_TASK.md"))
    assert remaining == ["笔记01_绪论_TASK.md", "笔记03_理论_TASK.md"], remaining


def test_cleanup_keeps_empty_transcript_as_pending(make_workspace):
    """空文件不算成品：把空壳当交付回收任务书，这一块就再也没人转录了。"""
    ws = _seed_cleanup_workspace(make_workspace)
    cleanup_completed_tasks(ws, keep_per_category=1, dry_run=False)
    remaining = sorted(p.name for p in ws.subtitles_dir.glob("BLK*_转录任务书.md"))
    assert remaining == ["BLK01_P01_转录任务书.md", "BLK03_P03_转录任务书.md"], remaining


def test_cleanup_reports_deleted_count_and_categories(make_workspace):
    """回收账本要分类可读（articles / notes / transcripts），否则日志看不出清了什么。"""
    ws = _seed_cleanup_workspace(make_workspace)
    result = cleanup_completed_tasks(ws, keep_per_category=1, dry_run=False)
    assert len(result["deleted"]) == 3, result["deleted"]
    assert set(result["counts"]) == {"articles", "notes", "transcripts"}


def test_cleanup_keeps_task_when_note_product_is_below_threshold(make_workspace):
    """成品判定按字节阈值：几百字节的 `_笔记.md` 是空壳，不能拿它回收任务书。"""
    ws = make_workspace("thin_note")
    write_text(ws.notes_dir / "笔记01_绪论_TASK.md", "t" * 200)
    write_text(ws.notes_dir / "笔记01_绪论_笔记.md", "太短了\n")
    result = cleanup_completed_tasks(ws, keep_per_category=0, dry_run=False)
    assert (ws.notes_dir / "笔记01_绪论_TASK.md").exists()
    assert result["deleted"] == [], result["deleted"]


# ---------------------------------------------------------------------------
# 归并提示词：不许集数配额、不许篇数锚点
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("banned", ["1 到 3 集", "极少数大型模块可包含 4 集", "通常包含"])
def test_note_plan_prompt_bans_episode_quota(banned):
    """按集数配额归并会把同一知识体系硬切成几篇，归并粒度必须由内容决定。"""
    assert banned not in P.NOTE_PLAN_PROMPT


def test_note_plan_prompt_states_merge_principle():
    assert "宁可少而厚" in P.NOTE_PLAN_PROMPT


def test_note_plan_prompt_allows_multi_block_note():
    """不写明「一篇可以装多个块」，Agent 就会退化成机械的一块一篇。"""
    assert "一篇笔记可以装多个块" in P.NOTE_PLAN_PROMPT


@pytest.mark.parametrize(
    "篇数锚点", ["经验值", "十几篇", "二十来篇", "篇笔记通常", "通常落成"]
)
def test_note_plan_prompt_has_no_volume_anchor(篇数锚点):
    """篇数锚点是「1~3 集」硬约束的翻版：给了数字，Agent 就照着凑数或照着了事。"""
    assert 篇数锚点 not in P.NOTE_PLAN_PROMPT


# ---------------------------------------------------------------------------
# 归并提示词渲染：块标题 + 模块长文主题（后验主题）
# ---------------------------------------------------------------------------

_MERGE_BLOCKS = [
    {"block_id": 1, "title": "HTML入门、常用标签与表单", "span": "P01-P02",
     "episodes": [1, 2], "duration_min": 46.0},
    {"block_id": 2, "title": "CSS选择器与盒模型", "span": "P03",
     "episodes": [3], "duration_min": 54.0},
]


def _merge_prompt() -> str:
    return P.build_note_planning_prompt(
        _MERGE_BLOCKS, course_title="测试课", block_titles={1: "HTML 基础与表单"}
    )


def test_note_planning_prompt_renders_block_list():
    prompt = _merge_prompt()
    assert "块 01" in prompt and "HTML入门、常用标签与表单" in prompt


def test_note_planning_prompt_uses_module_article_theme():
    """归并依据是模块长文的 H1（读完后提炼的主题），不是分集标题。"""
    assert "模块长文主题：《HTML 基础与表单》" in _merge_prompt()


def test_note_planning_prompt_renders_span_and_duration():
    prompt = _merge_prompt()
    assert "P01–P02" in prompt and "46 分钟" in prompt


# ---------------------------------------------------------------------------
# 归并总入口：缺归并不终止，且绝不改写盘上的规划文件
# ---------------------------------------------------------------------------

def _merge_blocks():
    return [
        {"block_id": 1, "title": "甲", "span": "P01-P02", "episodes": [1, 2]},
        {"block_id": 2, "title": "乙", "span": "P03", "episodes": [3]},
    ]


def _merge_parts():
    return [{"page": 1, "title": "a"}, {"page": 2, "title": "b"}, {"page": 3, "title": "c"}]


def test_resolve_notes_falls_back_to_one_note_per_block(make_workspace):
    """缺归并时按「一块一篇」兜底继续（不终止），并导出归并任务书。"""
    ws = make_workspace("merge_task")
    ws.save_parts(_merge_parts())
    notes, status, _ = P.resolve_notes(_merge_blocks(), _merge_parts(), course_title="课", ws=ws)
    assert status == "unmerged", status
    assert len(notes) == 2 and notes[0]["episodes"] == [1, 2], notes
    assert (ws.root_dir / "note_plan_TASK.md").exists()


def test_resolve_notes_fallback_never_writes_plan_file(make_workspace):
    """兜底结果只在内存里用：回写成 note_plan.json 会把「未归并」伪装成 Agent 的规划。"""
    ws = make_workspace("merge_task")
    ws.save_parts(_merge_parts())
    P.resolve_notes(_merge_blocks(), _merge_parts(), course_title="课", ws=ws)
    assert not (ws.root_dir / "note_plan.json").exists()


def test_resolve_notes_adopts_valid_plan(make_workspace):
    """盘上有合法归并就必须采纳，并把跨块的集号推导齐全。"""
    ws = make_workspace("merge_task")
    ws.save_parts(_merge_parts())
    write_text(
        ws.root_dir / "note_plan.json",
        json.dumps(
            [{"note_id": 1, "note_title": "合并篇", "blocks": [1, 2], "core_theme": "y"}],
            ensure_ascii=False,
        ),
    )
    notes, status, _ = P.resolve_notes(_merge_blocks(), _merge_parts(), course_title="课", ws=ws)
    assert status == "planned" and len(notes) == 1
    assert notes[0]["episodes"] == [1, 2, 3]


def test_load_blocks_is_empty_without_block_plan(make_workspace):
    """根块计划是模块的唯一来源：没有 `block_plan.json` 就不得按集号硬切。"""
    ws = make_workspace("no_blocks")
    assert P.load_blocks(ws) == []


# ---------------------------------------------------------------------------
# 笔记产物命名与旧命名不再兼容
# ---------------------------------------------------------------------------

_NOTE_META = {
    "block_id": 4, "block_title": "数据容器体系", "episodes": [28, 46],
    "core_theme": "x", "blocks": [11, 12, 13],
}


def test_note_task_filename_uses_note_prefix():
    assert BlockSynthesizer.get_task_filename(_NOTE_META) == "笔记04_数据容器体系_TASK.md"


def test_note_product_filename_matches_task_filename():
    assert BlockSynthesizer.get_note_filename(_NOTE_META) == "笔记04_数据容器体系_笔记.md"


def test_blocks_str_lists_every_claimed_block():
    """笔记跨多个块时必须在任务书里写明，否则 Agent 会照一个块去写。"""
    assert BlockSynthesizer._blocks_str(_NOTE_META) == "块 11、块 12、块 13"


def test_legacy_module_prefixed_note_is_not_reclaimed(make_workspace):
    """旧命名 `模块XX_…_笔记.md` 不再兼容：认领它会让归并后的新笔记永远派不出去。"""
    ws = make_workspace("note_lookup")
    write_text(ws.notes_dir / "模块04_字面量变量与标识符命名规范_笔记.md", pad_to("旧粒度成品"))
    assert find_module_note(ws, 4) is None


# ---------------------------------------------------------------------------
# 集号基准与课程标题：以工作区为准
# ---------------------------------------------------------------------------

def test_scope_parts_uses_workspace_parts(make_workspace):
    """在线集合永远返回全集；拿它当基准会把用户自己的区间判成非法。"""
    from src.pipeline import resolve_scope_parts

    ws = make_workspace("黑马课程_BV1sHU9BmEne")
    ws.save_parts([{"page": p, "title": f"第{p}讲"} for p in PAGES])
    ws.save_manifest({"title": "工作区标题", "bvid": "BV1sHU9BmEne"})
    info = {
        "title": "在线标题", "bvid": "BV1sHU9BmEne",
        "parts": [{"page": p, "title": f"在线第{p}讲"} for p in range(1, 186)],
    }
    scoped = resolve_scope_parts(info, ws)
    assert len(scoped) == 79 and scoped[0]["page"] == 9, scoped[:1]


def test_course_title_prefers_workspace_manifest(make_workspace):
    """标题取错会让 Agent 对着别的课名做规划。"""
    from src.pipeline import resolve_course_title

    ws = make_workspace("黑马课程_BV1sHU9BmEne")
    ws.save_manifest({"title": "工作区标题", "bvid": "BV1sHU9BmEne"})
    info = {"title": "在线标题", "bvid": "BV1sHU9BmEne", "parts": []}
    assert resolve_course_title(info, ws) == "工作区标题"


# ---------------------------------------------------------------------------
# 旧粒度任务书作废：粒度变了以后不能再被照着派发
# ---------------------------------------------------------------------------

def test_prune_superseded_tasks_removes_old_granularity(make_workspace):
    """粒度从「一块一篇」变成「一篇装多块」后，旧任务书留在盘上会被照着再派一批错位笔记。"""
    ws = make_workspace("prune_task")
    ws.save_parts([{"page": 1, "title": "a"}, {"page": 2, "title": "b"}])
    for name in ("笔记01_旧主题A_TASK.md", "笔记02_旧主题B_TASK.md"):
        write_text(ws.notes_dir / name, "旧粒度任务书")
    merged_notes = [{"note_id": 1, "note_title": "合并篇", "blocks": [1, 2], "episodes": [1, 2]}]
    removed = BlockSynthesizer._prune_superseded_tasks(ws, merged_notes)
    assert removed == 2, removed
    assert not list(ws.notes_dir.glob("笔记*_TASK.md"))


def test_prune_superseded_tasks_keeps_task_with_product(make_workspace):
    """成品已落盘的任务书是交付记录，交给 cleanup 回收，绝不在这里删。"""
    ws = make_workspace("prune_task")
    ws.save_parts([{"page": 1, "title": "a"}, {"page": 2, "title": "b"}])
    write_text(ws.notes_dir / "笔记01_旧主题A_TASK.md", "旧粒度任务书")
    write_text(ws.notes_dir / "笔记01_旧主题A_笔记.md", pad_to("成品"))
    merged_notes = [{"note_id": 1, "note_title": "合并篇", "blocks": [1, 2], "episodes": [1, 2]}]
    assert BlockSynthesizer._prune_superseded_tasks(ws, merged_notes) == 0
    assert (ws.notes_dir / "笔记01_旧主题A_TASK.md").exists()


# ---------------------------------------------------------------------------
# 端到端：缺归并时 CLI 照常收尾，且不回写规划文件
# ---------------------------------------------------------------------------

def _local_source_dir(tmp_path: Path) -> Path:
    """给 CLI 一个**本地**媒体目录当入口：解析走 local provider，全程离线。"""
    src = tmp_path / "本地课程源"
    src.mkdir(parents=True, exist_ok=True)
    (src / "第01讲.mp4").write_bytes(b"")
    return src


def test_cluster_notes_cli_survives_missing_plan(
    run_cli, make_parts, blocks_factory, tmp_path
):
    """端到端：缺归并时以 0 退出、导出归并任务书，且 note_plan.json 一个字节都不写。"""
    ws = make_parts(pages=(1, 2), task_name="笔记CLI探针")
    write_blocks(ws, [blocks_factory(1, [1, 2], "第1讲 甲")])
    res = run_cli(
        "cluster-notes", str(_local_source_dir(tmp_path)),
        "--task", ws.root_dir.name, "--base-dir", str(ws.base_dir),
    )
    assert res.code == 0, res.out[-500:]
    assert "尚无笔记归并规划" in res.out, res.out[-500:]
    assert (ws.root_dir / "note_plan_TASK.md").exists()
    assert not (ws.root_dir / "note_plan.json").exists()
