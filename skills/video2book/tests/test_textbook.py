# -*- coding: utf-8 -*-
"""教材整编（`src/generator/integrator.py`）的行为契约。

迁移自 `scripts/selfcheck.py::check_integrator_no_hardcoded_course`（502-516），
并按 `references/delivery_matrix.md`「产物 C：模块合辑教材」与源码的真实口径补齐：

* `validate_plan` —— 块恰好被认领一次、册名非空且不空泛；
* `salvage_plan` —— 不完全合法的规划就地抢救成全覆盖（只在内存里用，不落盘）；
* 整编本身 —— 章标题取块长文 H1、标题先幂等去号再整体降级、同名章消歧、
  缺长文的块不落占位册、超 300KB 按块边界继续切分。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from conftest import pad_to, write_blocks, write_text

from src.generator.integrator import ArticleIntegrator, PlanError

COURSE = "探针课"


# ---------------------------------------------------------------------------
# 就地小工具
# ---------------------------------------------------------------------------

def _integrator(ws) -> ArticleIntegrator:
    return ArticleIntegrator(ws.root_dir)


def _article(ws, block_id: int, file_title: str, body: str) -> Path:
    """写一篇模块长文：`articles/模块XX_<file_title>_精读长文.md`。"""
    return write_text(ws.articles_dir / f"模块{block_id:02d}_{file_title}_精读长文.md", body)


def _article_with_h1(ws, block_id: int, file_title: str, h1: str, tail: str = "") -> Path:
    """写一篇带 H1 主题的模块长文（正文补足到成品阈值以上）。"""
    body = pad_to(f"# {h1}\n\n{tail or '正文内容。'}")
    return _article(ws, block_id, file_title, body)


def _bulk(text: str, target_bytes: int) -> str:
    """把正文补到指定字节数——体量兜底用例要的是**真实文件体积**。"""
    body = text
    while len(body.encode("utf-8")) < target_bytes:
        body += "A" * 4096 + "\n"
    return body


def _lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _plan_blocks() -> List[Dict[str, Any]]:
    return [
        {"block_id": 1, "title": "甲", "span": "P01", "episodes": [1], "duration_min": 46.0},
        {"block_id": 2, "title": "乙", "span": "P02", "episodes": [2], "duration_min": 46.0},
        {"block_id": 3, "title": "丙", "span": "P03", "episodes": [3], "duration_min": 46.0},
    ]


def _plan_titles(groups) -> List[str]:
    return [title for title, _ in groups]


def _plan_ids(groups) -> List[List[int]]:
    return [[int(b.get("block_id") or 0) for b in members] for _, members in groups]


# ---------------------------------------------------------------------------
# 迁移项：整编器不得内嵌具体课程数据
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "needle", ["微机原理", "8253", "8255A", "黑马程序员", "核心语法-", "函数基础"]
)
def test_integrator_source_has_no_hardcoded_course_data(skill_root, needle):
    """写死某门课的语料特征会让整编器只对那门课成立，换课即整编出错。"""
    text = (skill_root / "src" / "generator" / "integrator.py").read_text(encoding="utf-8")
    assert needle not in text, f"integrator.py 仍含硬编码课程数据: {needle}"


def test_integrator_run_requires_course_title():
    """课程标题是必传参数：有默认值就会静默用错课名写进册首与目录。"""
    import inspect

    sig = inspect.signature(ArticleIntegrator.run)
    assert sig.parameters["course_title"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# validate_plan：块恰好被认领一次、册名非空且不空泛
# ---------------------------------------------------------------------------

def test_validate_plan_accepts_full_single_claim():
    """每块恰好进一册、册名能看出内容 → 合法。"""
    plan = [
        {"volume_id": 1, "volume_title": "甲与乙：基础篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "丙：进阶篇", "blocks": [3]},
    ]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert ok, reason


def test_validate_plan_rejects_empty_plan():
    """空规划当合法会让整编器直接产不出教材却报成功。"""
    ok, reason = ArticleIntegrator.validate_plan([], _plan_blocks())
    assert not ok and "为空" in reason, reason


def test_validate_plan_rejects_volume_without_title():
    """册名就是教材文件名，缺了它就没有可读的册名。"""
    plan = [{"volume_id": 1, "volume_title": "   ", "blocks": [1, 2, 3]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "volume_title" in reason, reason


@pytest.mark.parametrize("vague", ["第一部分", "综合模块", "其他", "未分类"])
def test_validate_plan_rejects_vague_volume_title(vague):
    """空泛册名看不出内容——册名本身就是教材的目录。"""
    plan = [{"volume_id": 1, "volume_title": vague, "blocks": [1, 2, 3]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "空泛册名" in reason, reason


def test_validate_plan_rejects_volume_without_blocks():
    plan = [
        {"volume_id": 1, "volume_title": "甲篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "空册", "blocks": []},
    ]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "未认领任何块" in reason, reason


def test_validate_plan_rejects_unknown_block():
    """引用不存在的块会让某一段真实语料永远进不了书。"""
    plan = [
        {"volume_id": 1, "volume_title": "甲篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "丙篇", "blocks": [3, 99]},
    ]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "不存在" in reason, reason


def test_validate_plan_rejects_duplicate_claim():
    """同一块被两册认领 = 同一段内容在书里出现两遍，章序也失去意义。"""
    plan = [
        {"volume_id": 1, "volume_title": "甲篇", "blocks": [1, 2]},
        {"volume_id": 2, "volume_title": "乙篇", "blocks": [2, 3]},
    ]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "重复认领" in reason, reason


def test_validate_plan_rejects_unclaimed_block():
    """漏认领的块会凭空从教材里消失（长文还在 articles/，但读者看不到）。"""
    plan = [{"volume_id": 1, "volume_title": "甲篇", "blocks": [1, 2]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "未被任何册认领" in reason, reason


def test_validate_plan_rejects_non_numeric_block_id():
    plan = [{"volume_id": 1, "volume_title": "甲篇", "blocks": [1, "甲"]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "非法" in reason, reason


# ---------------------------------------------------------------------------
# salvage_plan：不完全合法的规划就地抢救成全覆盖
# ---------------------------------------------------------------------------

def test_salvage_plan_fills_orphans_into_one_fallback_volume():
    """孤块不得丢：被漏掉的块补成一册兜底（封面册名取首块标题），并给出诊断行。"""
    plan = [{"volume_title": "甲册", "blocks": [1]}]
    groups, diag = ArticleIntegrator.salvage_plan(plan, _plan_blocks())
    assert _plan_titles(groups)[0] == "甲册"
    assert _plan_ids(groups) == [[1], [2, 3]], _plan_ids(groups)
    assert "02、03" in diag[0], diag


def test_salvage_plan_claims_first_come_first_served():
    """重复认领不得让同一块进两册：先到先得，后来的丢弃，并且**要说清**丢了什么。"""
    plan = [{"volume_title": "甲册", "blocks": [1, 1]}]
    groups, diag = ArticleIntegrator.salvage_plan(plan, [{"block_id": 1, "title": "甲", "episodes": [1]}])
    assert _plan_ids(groups) == [[1]], _plan_ids(groups)
    # 第二阶段 A2：以前这里是静默丢弃，`run()` 打出「已就地抢救后继续：」后面什么都没有，
    # 用户完全无从知道哪一块被吞了。
    assert len(diag) == 1 and "块 01" in diag[0] and "出现多次" in diag[0], diag


def test_salvage_plan_reports_cross_volume_duplicate():
    """跨册重复认领要说清「谁保留了、谁被丢了」（第二阶段 A2）。"""
    plan = [
        {"volume_title": "甲册", "blocks": [1]},
        {"volume_title": "乙册", "blocks": [1]},
    ]
    groups, diag = ArticleIntegrator.salvage_plan(
        plan, [{"block_id": 1, "title": "甲", "episodes": [1]}]
    )
    assert _plan_ids(groups) == [[1]], _plan_ids(groups)
    assert any(
        "块 01" in line and "第 2 册" in line and "第 1 册" in line for line in diag
    ), diag


def test_salvage_plan_ignores_unknown_block_and_reports():
    """引用了不存在的块只忽略并记账，绝不因此丢掉真块。"""
    plan = [{"volume_title": "甲册", "blocks": [1, 99]}]
    groups, diag = ArticleIntegrator.salvage_plan(plan, [{"block_id": 1, "title": "甲", "episodes": [1]}])
    assert _plan_ids(groups) == [[1]], _plan_ids(groups)
    assert "引用了不存在的块" in diag[0] and "99" in diag[0], diag


def test_salvage_plan_uses_group_title_when_volume_title_blank():
    """册名不能是空的：规划没给名字时按块标题兜底起名。"""
    plan = [{"volume_title": "   ", "blocks": [1, 2]}]
    groups, _ = ArticleIntegrator.salvage_plan(plan, _plan_blocks()[:2])
    assert _plan_titles(groups) == ["甲 等 2 块"], _plan_titles(groups)


def test_salvage_plan_drops_volume_with_no_usable_block():
    """只引用不存在块的册会被整个丢掉（不能留下一册空壳）。"""
    plan = [{"volume_title": "空壳册", "blocks": [99]}]
    groups, _ = ArticleIntegrator.salvage_plan(plan, _plan_blocks()[:2])
    assert _plan_titles(groups) == ["甲 等 2 块"], _plan_titles(groups)


def test_salvage_plan_orders_groups_and_blocks_by_block_id():
    """抢救结果按块号排序，跨册章序才不会乱。"""
    plan = [
        {"volume_title": "乙册", "blocks": [3]},
        {"volume_title": "甲册", "blocks": [2, 1]},
    ]
    groups, _ = ArticleIntegrator.salvage_plan(plan, _plan_blocks())
    assert _plan_titles(groups) == ["甲册", "乙册"], _plan_titles(groups)
    assert _plan_ids(groups) == [[1, 2], [3]], _plan_ids(groups)


# ---------------------------------------------------------------------------
# 分册规划落到盘上：Agent 规划优先 / 缺规划导出任务书 / 不合法就地抢救
# ---------------------------------------------------------------------------

def test_run_adopts_valid_plan_volume_titles(make_workspace, blocks_factory):
    """Agent 规划的分册与册名必须原样落地：它是教材的目录。"""
    ws = make_workspace("plan_probe")
    blocks = [
        blocks_factory(1, [1], "甲", span="P01"),
        blocks_factory(2, [2], "乙", span="P02"),
        blocks_factory(3, [3], "丙", span="P03"),
    ]
    write_blocks(ws, blocks)
    for bid, title in ((1, "甲"), (2, "乙"), (3, "丙")):
        _article_with_h1(ws, bid, title, f"{title}主题")
    write_text(
        ws.root_dir / "textbook_plan.json",
        json.dumps(
            [
                {"volume_id": 1, "volume_title": "甲与乙：基础篇", "blocks": [1, 2]},
                {"volume_id": 2, "volume_title": "丙：进阶篇", "blocks": [3]},
            ],
            ensure_ascii=False,
        ),
    )
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    assert [b.name for b in books] == [
        "模块01_甲与乙：基础篇_精读全书.md", "模块02_丙：进阶篇_精读全书.md",
    ], [b.name for b in books]
    text = books[0].read_text(encoding="utf-8")
    assert "## 甲主题" in text and "## 乙主题" in text and "## 丙主题" not in text


def test_run_exports_plan_task_when_plan_missing(make_workspace, blocks_factory):
    """缺规划不卡流程，但要导出分册规划任务书，让 Agent 补上能看出内容的册名。"""
    ws = make_workspace("plan_task_probe")
    blocks = [blocks_factory(1, [1], "甲", span="P01")]
    write_blocks(ws, blocks)
    _article_with_h1(ws, 1, "甲", "甲主题")
    _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    task = ws.root_dir / "textbook_plan_TASK.md"
    assert task.exists()
    assert "块 01" in task.read_text(encoding="utf-8")


# ── 第二阶段 A1：册序只认数组位置，volume_id 只做校验 ──────────────────────

def test_validate_plan_rejects_non_integer_volume_id():
    """`volume_id` 非法要报出**册序号与收到的值**，不能让它变成后面的 KeyError。"""
    plan = [{"volume_id": "甲", "volume_title": "甲册", "blocks": [1, 2, 3]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "volume_id" in reason, reason


def test_validate_plan_rejects_volume_id_below_one():
    """`volume_id` 从 1 开始：`0` 既非法也是假值，最容易被 `or index` 静默改写。"""
    plan = [{"volume_id": 0, "volume_title": "甲册", "blocks": [1, 2, 3]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "volume_id" in reason, reason


def test_validate_plan_rejects_duplicate_volume_id():
    """重复的 `volume_id` 会让「按 id 建字典」丢掉一册，必须判死。"""
    plan = [
        {"volume_id": 1, "volume_title": "甲册", "blocks": [1]},
        {"volume_id": 1, "volume_title": "乙册", "blocks": [2, 3]},
    ]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert not ok and "volume_id" in reason, reason


def test_validate_plan_accepts_plan_without_volume_id():
    """`volume_id` 缺失不算错：册序以数组位置为准，不能把老规划一律判死。"""
    plan = [{"volume_title": "甲册", "blocks": [1, 2, 3]}]
    ok, reason = ArticleIntegrator.validate_plan(plan, _plan_blocks())
    assert ok, reason


def test_run_reads_volumes_positionally_not_by_volume_id(make_workspace, blocks_factory):
    """册序只认**数组位置**。

    第二阶段 A1：以前 `selection` 用 `volume_id` 作键、读取却用位置下标 `selection[index]`。
    于是 `volume_id` 取 10/20 时抛未捕获的 `KeyError: 1`；而写成乱序（如 `[2, 1]`）时
    两个键都在、不报错，却把册**静默错配**成另一册的块。这里用「乱序 id」验证位置说了算。
    """
    ws = make_workspace("plan_positional_probe")
    blocks = [
        blocks_factory(1, [1], "甲", span="P01"),
        blocks_factory(2, [2], "乙", span="P02"),
    ]
    write_blocks(ws, blocks)
    for bid, title in ((1, "甲"), (2, "乙")):
        _article_with_h1(ws, bid, title, f"{title}主题")
    write_text(
        ws.root_dir / "textbook_plan.json",
        json.dumps(
            [
                # id 与位置故意错开：位置是唯一真源，所以第 1 册就是「乙：先讲」
                {"volume_id": 20, "volume_title": "乙：先讲", "blocks": [2]},
                {"volume_id": 10, "volume_title": "甲：后讲", "blocks": [1]},
            ],
            ensure_ascii=False,
        ),
    )
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    assert [b.name for b in books] == [
        "模块01_乙：先讲_精读全书.md", "模块02_甲：后讲_精读全书.md",
    ], [b.name for b in books]
    first = books[0].read_text(encoding="utf-8")
    assert "## 乙主题" in first and "## 甲主题" not in first, "册与块错配"


@pytest.mark.parametrize(
    "raw, needle",
    [
        ("{ 这不是 JSON", "不是合法 JSON"),
        ('{"volume_id": 1}', "必须是 JSON Array"),
        ('["甲册", "乙册"]', "没有一个是合法分册对象"),
    ],
)
def test_load_plan_raises_on_unusable_file(make_workspace, raw: str, needle: str):
    """坏规划文件必须报出来。

    第二阶段 A1：以前坏文件被静默当成「没有规划」，Agent 写坏一个字符就悄悄退回
    兜底分册，用户完全不知道自己的规划没生效。
    """
    ws = make_workspace("plan_broken_probe")
    write_text(ws.root_dir / "textbook_plan.json", raw)
    with pytest.raises(PlanError, match=needle):
        _integrator(ws).load_plan()


def test_load_plan_missing_file_is_not_an_error(make_workspace):
    """文件不存在是正常情况（走内容结构兜底），不能抛。"""
    ws = make_workspace("plan_absent_probe")
    assert _integrator(ws).load_plan() == []


def test_run_does_not_rewrite_invalid_plan_file(make_workspace, blocks_factory, capsys):
    """不合法规划就地抢救后继续，但盘上那份**一个字节都不改**（留给 Agent 修）。"""
    ws = make_workspace("salvage_probe")
    blocks = [
        blocks_factory(1, [1], "甲", span="P01"),
        blocks_factory(2, [2], "乙", span="P02"),
    ]
    write_blocks(ws, blocks)
    for bid, title in ((1, "甲"), (2, "乙")):
        _article_with_h1(ws, bid, title, f"{title}主题")
    plan_file = write_text(
        ws.root_dir / "textbook_plan.json",
        json.dumps([{"volume_id": 1, "volume_title": "甲册", "blocks": [1]}], ensure_ascii=False),
    )
    before = plan_file.read_text(encoding="utf-8")
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    assert plan_file.read_text(encoding="utf-8") == before
    joined = "".join(b.read_text(encoding="utf-8") for b in books)
    assert "## 甲主题" in joined and "## 乙主题" in joined, "抢救后漏了块"
    assert "抢救" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 整编：章标题、去号降级、消歧、gate、体量切分
# ---------------------------------------------------------------------------

def test_run_uses_article_h1_as_chapter_title(make_workspace, blocks_factory):
    """章标题取长文 H1（写作者提炼的主题），不是带平台编号的分集名。"""
    ws = make_workspace("h1_probe")
    blocks = [
        blocks_factory(1, [5], "数据库第2章 关系数据库 （上）", span="P05"),
    ]
    write_blocks(ws, blocks)
    _article_with_h1(
        ws, 1, "数据库第2章 关系数据库 （上）", "关系模型结构与数据完整性约束",
        tail="## 关系数据结构\n\n正文内容。",
    )
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    text = books[0].read_text(encoding="utf-8")
    assert "## 关系模型结构与数据完整性约束" in text
    assert "1. 关系模型结构与数据完整性约束（P05）" in text


def test_run_keeps_episode_name_in_block_trace(make_workspace, blocks_factory):
    """分集名退到「对应块」备注里：按集溯源时还得找得到原始分集。"""
    ws = make_workspace("h1_probe")
    blocks = [blocks_factory(1, [5], "数据库第2章 关系数据库 （上）", span="P05")]
    write_blocks(ws, blocks)
    _article_with_h1(ws, 1, "数据库第2章 关系数据库 （上）", "关系模型结构与数据完整性约束")
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    text = books[0].read_text(encoding="utf-8")
    assert "块标题（分集名）：《数据库第2章 关系数据库 （上）》" in text


def test_run_strips_heading_number_before_demoting(make_workspace, blocks_factory):
    """长文标题继承进来时先幂等去号、再整体降一级，免得与阅读器自动编号叠成双号。"""
    ws = make_workspace("demote_probe")
    blocks = [blocks_factory(1, [1], "甲", span="P01")]
    write_blocks(ws, blocks)
    _article(
        ws, 1, "甲",
        pad_to(
            "# 主题\n\n正文第一段。\n\n## 2.1 关系模型\n\n内容。\n\n"
            "## 关系数据结构\n\n内容。\n\n### 3.7 点击 Install\n\n内容。\n"
        ),
    )
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    lines = _lines(books[0])
    assert "### 关系模型" in lines, lines
    assert "## 2.1 关系模型" not in lines, lines
    assert "### 关系数据结构" in lines, lines
    assert "#### 点击 Install" in lines, lines


def test_run_demotion_is_idempotent(make_workspace, blocks_factory):
    """去号幂等：重跑整编不得把标题再降一级或留下一层多余序号。"""
    ws = make_workspace("idempotent_probe")
    blocks = [blocks_factory(1, [1], "甲", span="P01")]
    write_blocks(ws, blocks)
    _article(
        ws, 1, "甲",
        pad_to("# 主题\n\n正文第一段。\n\n## 2.1 关系模型\n\n内容。\n\n## 关系数据结构\n\n内容。\n"),
    )
    first = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)[0]
    once = first.read_text(encoding="utf-8")
    second = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)[0]
    assert second.read_text(encoding="utf-8") == once


def test_run_disambiguates_same_named_chapters(make_workspace, blocks_factory):
    """劈分腿会让两章同名（长文都叫同一个主题）；不消歧就会出现「上一节讲完 A，下一节讲 A」。"""
    ws = make_workspace("dup_probe")
    blocks = [
        blocks_factory(1, [6], "关系数据库（下）", span="P06上"),
        blocks_factory(2, [6], "关系数据库（下）", span="P06下"),
    ]
    write_blocks(ws, blocks)
    for bid in (1, 2):
        _article_with_h1(ws, bid, "关系数据库（下）", "关系数据库（下）")
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    text = books[0].read_text(encoding="utf-8")
    assert "## 关系数据库（下）（P06上）" in text
    assert "## 关系数据库（下）（P06下）" in text
    assert "1. 关系数据库（下）（P06上）" in text
    assert "上一节讲完「关系数据库（下）（P06上）」，下一节接着讲「关系数据库（下）（P06下）」" in text
    assert "（P06上）（P06上）" not in text and "（P06下）（P06下）" not in text
    assert "共 1 讲" in text, "讲数未按去重集号统计（劈分腿被重复计数）"


def test_run_skips_block_without_article(make_workspace, blocks_factory, capsys):
    """缺模块长文的块不进书（打 gate），也不落占位册——占位册会被读者当成真内容。"""
    ws = make_workspace("gate_probe")
    blocks = [
        blocks_factory(1, [1], "甲", span="P01"),
        blocks_factory(2, [2], "乙", span="P02"),
    ]
    write_blocks(ws, blocks)
    _article_with_h1(ws, 1, "甲", "甲主题")
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    assert len(books) == 1, [b.name for b in books]
    assert sorted(p.name for p in (ws.root_dir / "textbooks").glob("*_精读全书.md")) == [
        books[0].name
    ]
    text = books[0].read_text(encoding="utf-8")
    assert "## 乙主题" not in text and "⚠️" not in text
    assert "尚无模块长文" in capsys.readouterr().out


def test_run_splits_oversized_volume_at_block_boundary(make_workspace, blocks_factory):
    """一册超 300KB 时在块边界再切（块绝不跨册拆开），否则一本几 MB 的书在阅读器里翻不动。"""
    ws = make_workspace("cap_probe")
    blocks = [
        blocks_factory(1, [1], "甲", span="P01"),
        blocks_factory(2, [2], "乙", span="P02"),
        blocks_factory(3, [3], "丙", span="P03"),
    ]
    write_blocks(ws, blocks)
    for bid, title in ((1, "甲"), (2, "乙"), (3, "丙")):
        # 每篇 ~120KB：三篇合计超过默认 300KB 上限
        _article(ws, bid, title, f"# {title}主题\n\n" + _bulk("正文。\n", 120_000))
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    assert len(books) == 2, [b.name for b in books]
    assert "（上）" in books[0].name and "（下）" in books[1].name, [b.name for b in books]
    first, second = (b.read_text(encoding="utf-8") for b in books)
    assert "## 甲主题" in first and "## 乙主题" in first
    assert "## 丙主题" not in first and "## 丙主题" in second


def test_run_removes_stale_volumes(make_workspace, blocks_factory):
    """教材是纯派生：本轮不再产出的旧册必须清掉，否则读者会拿到过期版本。"""
    ws = make_workspace("stale_probe")
    blocks = [blocks_factory(1, [1], "甲", span="P01")]
    write_blocks(ws, blocks)
    _article_with_h1(ws, 1, "甲", "甲主题")
    stale = write_text(ws.root_dir / "textbooks" / "模块09_旧册_精读全书.md", pad_to("旧册正文"))
    books = _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True)
    assert len(books) == 1
    assert not stale.exists(), "不再产出的旧册未被清理"


def test_run_removes_stale_volumes_even_when_nothing_is_ready(make_workspace, blocks_factory):
    """一篇长文都不在时**也要**清陈旧全书。

    第二阶段 A8：`run()` 在 `ready` 为空时直接 `return []`，`_remove_stale_volumes`
    永不执行——删掉最后一篇长文后，`textbooks/模块01_绪论_精读全书.md` 会作为陈旧产物
    永远留在盘上。
    """
    ws = make_workspace("stale_empty_probe")
    blocks = [blocks_factory(1, [1], "甲", span="P01")]
    write_blocks(ws, blocks)
    # 故意不写任何模块长文：ready 为空
    stale = write_text(ws.root_dir / "textbooks" / "模块01_绪论_精读全书.md", pad_to("旧册正文"))
    assert _integrator(ws).run(course_title=COURSE, blocks=blocks, force=True) == []
    assert not stale.exists(), "ready 为空时陈旧全书没被清理"


# ---------------------------------------------------------------------------
# 端到端：CLI 退出码与成品落盘
# ---------------------------------------------------------------------------

def _local_source_dir(tmp_path: Path) -> Path:
    """给 CLI 一个**本地**媒体目录当入口：解析走 local provider，全程离线。"""
    src = tmp_path / "本地课程源"
    src.mkdir(parents=True, exist_ok=True)
    (src / "第01讲.mp4").write_bytes(b"")
    return src


def test_cluster_articles_cli_exits_2_without_blocks(run_cli, make_parts, tmp_path):
    """没有块清单就没有模块可整编：明确以退出码 2 停下并给出装箱命令，而不是产出空书。"""
    ws = make_parts(pages=(1,), task_name="教材CLI无块")
    res = run_cli(
        "cluster-articles", str(_local_source_dir(tmp_path)),
        "--task", ws.root_dir.name, "--base-dir", str(ws.base_dir),
    )
    assert res.code == 2, res.out[-500:]
    assert "merge-audio" in res.out


def test_cluster_articles_cli_writes_volume(
    run_cli, make_parts, blocks_factory, tmp_path
):
    """端到端：整编以 0 退出、册落进 textbooks/，且原 articles/ 一字不动。"""
    ws = make_parts(pages=(1,), task_name="教材CLI探针")
    blocks = [blocks_factory(1, [1], "甲", span="P01")]
    write_blocks(ws, blocks)
    article = _article_with_h1(ws, 1, "甲", "甲主题")
    res = run_cli(
        "cluster-articles", str(_local_source_dir(tmp_path)),
        "--task", ws.root_dir.name, "--base-dir", str(ws.base_dir),
    )
    assert res.code == 0, res.out[-500:]
    assert "教材已整编" in res.out, res.out[-500:]
    volumes = sorted(p.name for p in (ws.root_dir / "textbooks").glob("*_精读全书.md"))
    assert len(volumes) == 1 and volumes[0].startswith("模块01_"), volumes
    assert article.exists(), "整编动了 articles/ 里的原文"
