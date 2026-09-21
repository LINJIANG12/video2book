# -*- coding: utf-8 -*-
"""标题序号纪律：判定与去号同源、正文一字不碰、批量清理幂等。

迁自 `scripts/selfcheck.py` 的 `check_heading_number_discipline`。
背景（实测踩过两轮）：阅读器（Typora）会**自动**给标题编号，标题里再手写一套
（笔记的 `## 1. …`、教材的 `## 第 3 章：…` 与继承来的 `## 2.1 …`）就会叠成双号。
所以口径统一为「标题不写序号」，存量产物由 `check --fix-numbering` 就地清理。

规则的难点全在**不误伤**：`## 3 种方案的取舍`、`## 2025 年路线图`、
`#### 5.7 与 8.0 版本元数据呈现差异` 里的数字是内容不是序号，
剥离规则刻意收得极窄；一旦放宽，六门课语料里上百处合法标题会被削掉头。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import pad_to  # noqa: E402

from src.core import heading_cleanup  # noqa: E402
from src.core.deliverable_lint import lint_heading_numbers  # noqa: E402
from src.core.heading_numbers import is_numbered_heading, strip_heading_number  # noqa: E402

# (原始标题行, 去号后) —— 实测语料里的常见形态，含章号前缀与多级序号
STRIP_CASES = (
    ("## 1. 列表标签的三大分类", "## 列表标签的三大分类"),
    ("### 2.1 无序列表的语义", "### 无序列表的语义"),
    ("#### 1 这一阶段要拿下的三件事", "#### 这一阶段要拿下的三件事"),
    ("#### 1.3 大小写书写规范", "#### 大小写书写规范"),
    ("### 1 层次化的看问题方法", "### 层次化的看问题方法"),
    ("### 2 类属性", "### 类属性"),
    ("#### 3.7 点击 Install 并等待", "#### 点击 Install 并等待"),
    ("#### 1.1 年月日与时分秒的独立获取", "#### 年月日与时分秒的独立获取"),
    ("## 第 3 章：关系模型", "## 关系模型"),
    ("## 3、工程定位", "## 工程定位"),
)

# 数字是**内容**的标题：年份、错误码、计数词、并列连词、多值并列
CONTENT_CASES = (
    "## 3 种方案的取舍",
    "## 1.5 倍速播放",
    "## 2025 年路线图",
    "### 1963 年火星火箭：一句 Fortran 循环语句的录入错误",
    "#### 5.7 与 8.0 版本元数据呈现差异",
    "#### 0、1 与 NULL 的三值逻辑闭包",
    "# 篇名",
    "普通正文行",
)


# ---------------------------------------------------------------------------
# 去号规则本体（判定与剥离同源）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("before, after", STRIP_CASES)
def test_strip_heading_number_cases(before: str, after: str):
    """常见手写序号形态必须都能剥掉，且只动序号、不动标题文字。"""
    assert strip_heading_number(before) == after, f"去号失败: {before!r}"


@pytest.mark.parametrize("before, after", STRIP_CASES)
def test_strip_heading_number_is_idempotent(before: str, after: str):
    """去号必须幂等：`check --fix-numbering` 可反复跑，第二轮不得再吃到正文。"""
    assert strip_heading_number(after) == after, f"去号不幂等: {after!r}"


@pytest.mark.parametrize("line", CONTENT_CASES)
def test_content_numbers_are_kept(line: str):
    """内容型数字一律保留：这是全套规则里最容易被「优化」掉的一条护栏。"""
    assert strip_heading_number(line) == line, f"内容型数字被误剥: {line!r}"


@pytest.mark.parametrize("line", CONTENT_CASES)
def test_content_numbers_are_not_flagged_as_numbered(line: str):
    """判定与剥离同源：去号不动的行，门禁同样不得判它「带手写序号」。"""
    assert not is_numbered_heading(line), f"内容型数字被误判为序号: {line!r}"


def test_numbered_heading_is_flagged():
    """带序号的标题必须同时被判定与剥离认出来（判定若与剥离不同源，门禁就白设）。"""
    line = "## 1. 带序号的标题"
    assert is_numbered_heading(line), "带序号的标题未被判定为手写序号"
    assert strip_heading_number(line) == "## 带序号的标题"


def test_lint_heading_numbers_skips_fenced_lines():
    """围栏内的井号不是标题：代码块里的 `## 1.` 一旦计入，门禁会永远报假警。"""
    text = "```text\n## 1. 围栏内的井号不是标题\n```\n"
    assert lint_heading_numbers(text) == [], "围栏内的行被当成标题统计了"


def test_lint_heading_numbers_reports_line_number():
    """明细要带行号：存量清理时人眼复核全靠它定位。"""
    hits = lint_heading_numbers("# 篇名\n\n## 1. 第一节\n\n正文。\n")
    assert len(hits) == 1 and hits[0]["line"] == 3, f"行号不准确: {hits}"


# ---------------------------------------------------------------------------
# 批量清理内核 clean_text
# ---------------------------------------------------------------------------

SAMPLE = "# 篇名\n\n## 1. 第一节\n\n正文一。\n\n### 2.1 子节\n\n正文二。\n"


def test_clean_text_counts_stripped_headings():
    """计数是清理报告与幂等判据的基础，必须按标题行去号条数统计。"""
    _new_text, headings, toc, _changes, _suspects = heading_cleanup.clean_text(SAMPLE)
    assert (headings, toc) == (2, 0), f"去号条数不对：headings={headings} toc={toc}"


def test_clean_text_removes_heading_numbers():
    """标题序号必须真的从文本里消失，而不是只体现在计数上。"""
    new_text, *_ = heading_cleanup.clean_text(SAMPLE)
    assert "## 1." not in new_text and "### 2.1" not in new_text, "未剥掉标题序号"


def test_clean_text_leaves_body_untouched():
    """正文一字不碰：清理脚本最不可原谅的故障就是顺手改了内容。"""
    new_text, *_ = heading_cleanup.clean_text(SAMPLE)
    assert "正文一。" in new_text and "正文二。" in new_text, "动了正文"


def test_clean_text_is_idempotent():
    """第二遍必须零改动、零计数：清理脚本要能安全地反复重跑。"""
    new_text, *_ = heading_cleanup.clean_text(SAMPLE)
    again_text, headings2, toc2, _c2, _s2 = heading_cleanup.clean_text(new_text)
    assert (headings2, toc2) == (0, 0) and again_text == new_text, "去号不幂等"


def test_clean_text_normalizes_toc_chapter_line():
    """教材目录行 `- **第 N 章**：标题` 归一成有序列表 `N. 标题`，序号交给渲染器。"""
    toc_text, _headings, toc, _changes, _suspects = heading_cleanup.clean_text(
        "- **第 1 章**：绪论\n")
    assert toc_text == "1. 绪论\n" and toc == 1, f"教材目录行归一失败：{toc_text!r}"


def test_clean_text_skips_fenced_headings():
    """围栏内的行不动也不计数（代码块里的 `## 1.` 是注释，不是标题）。"""
    text = "# 篇名\n\n```text\n## 1. 不是标题\n```\n"
    new_text, headings, toc, _changes, _suspects = heading_cleanup.clean_text(text)
    assert (headings, toc) == (0, 0) and new_text == text, "围栏内的行被清理了"


def test_clean_text_reports_content_number_as_suspect():
    """被规则有意略过的「数字 + 内容信号」标题要单独报出来供人眼确认。"""
    _text, headings, _toc, _changes, suspects = heading_cleanup.clean_text(
        "## 3 种方案的取舍\n")
    assert headings == 0 and [s["number"] for s in suspects] == ["3"], \
        f"可疑行未被报道: {suspects}"


# ---------------------------------------------------------------------------
# 端到端：`check --fix-numbering` 就地清理且幂等
# ---------------------------------------------------------------------------

NUMBERED_ARTICLE = "# 篇名\n\n## 1. 第一节\n\n正文一。\n\n### 2.1 子节\n\n正文二。\n"

CLEAN_NOTE = (
    "# 数据结构与算法笔记\n\n"
    "## 数组的连续布局\n\n"
    "数组在内存里连续存放，按下标访问的代价是常数时间；链表的节点分散在堆上，插入只需改两处指针。\n\n"
    "## 复杂度取舍\n\n"
    "哈希表把平均查找压到常数时间，代价是冲突处理与扩容时的停顿。先看访问模式，再决定用哪个结构。\n"
)


def test_check_fix_numbering_rewrites_article_in_place(run_cli, make_workspace):
    """端到端：`check --fix-numbering` 必须就地改掉存量产物里的标题序号。"""
    ws = make_workspace("清理_就地去号")
    article = ws.articles_dir / "模块01_绪论与数制_精读长文.md"
    article.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    result = run_cli("check", "--fix-numbering", "--dir", str(ws.root_dir), "--json")
    assert result.code == 0, result.norm
    totals = json.loads(result.out)["totals"]
    assert totals["headings"] == 2 and totals["changed_files"] == 1, f"清理报告异常: {totals}"

    text = article.read_text(encoding="utf-8")
    assert "## 1." not in text and "### 2.1" not in text, "标题序号没被就地剥掉"
    assert "正文一。" in text and "正文二。" in text, "正文被改动"


def test_check_fix_numbering_is_idempotent(run_cli, make_workspace):
    """端到端：清理过的产物再跑一遍必须零改动 —— 这是它可以被反复调度的前提。"""
    ws = make_workspace("清理_幂等")
    article = ws.articles_dir / "模块01_绪论与数制_精读长文.md"
    article.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    assert run_cli("check", "--fix-numbering", "--dir", str(ws.root_dir)).code == 0
    after_first = article.read_text(encoding="utf-8")

    second = run_cli("check", "--fix-numbering", "--dir", str(ws.root_dir), "--json")
    assert second.code == 0, second.norm
    totals = json.loads(second.out)["totals"]
    assert totals["changed_files"] == 0 and totals["headings"] == 0, f"第二轮仍有改动: {totals}"
    assert article.read_text(encoding="utf-8") == after_first, "第二轮改动或改写了文件"


def test_check_fix_numbering_dry_run_leaves_file_alone(run_cli, make_workspace):
    """`--dry-run` 只报不改：预演若偷偷写盘，存量产物就没人敢先看一眼了。"""
    ws = make_workspace("清理_预演")
    article = ws.articles_dir / "模块01_绪论与数制_精读长文.md"
    article.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    result = run_cli("check", "--fix-numbering", "--dry-run", "--dir", str(ws.root_dir), "--json")
    assert result.code == 0, result.norm
    payload = json.loads(result.out)
    assert payload["dry_run"] is True
    assert payload["totals"]["headings"] == 2, f"预演未统计出待改行数: {payload['totals']}"
    assert article.read_text(encoding="utf-8") == NUMBERED_ARTICLE, "预演却写盘了"


def test_check_fix_numbering_skips_task_files(run_cli, make_workspace):
    """任务书不清理：它是派发物，改了名字与内容会让在途任务凭空错位。"""
    ws = make_workspace("清理_跳过任务书")
    task = ws.articles_dir / "模块01_绪论与数制_TASK.md"
    task.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    result = run_cli("check", "--fix-numbering", "--dir", str(ws.root_dir), "--json")
    assert result.code == 0, result.norm
    totals = json.loads(result.out)["totals"]
    assert totals["scanned_files"] == 0 and totals["headings"] == 0, f"任务书被清理: {totals}"
    assert task.read_text(encoding="utf-8") == NUMBERED_ARTICLE


def test_deliver_require_no_numbering_turns_numbering_into_a_gate(run_cli, make_workspace):
    """手写序号默认只提示；`--require-no-numbering` 一加上就必须真拦。"""
    ws = make_workspace("门禁_标题序号")
    note = ws.notes_dir / "笔记01_数组与链表_笔记.md"
    note.write_text(pad_to(CLEAN_NOTE.replace("## 数组的连续布局", "## 1. 数组的连续布局")),
                    encoding="utf-8")

    assert run_cli("check", "--deliver", "--strict", "--dir", str(ws.root_dir)).code == 0, \
        "手写序号默认是提示项，不该让 --strict 失败"
    strict = run_cli("check", "--deliver", "--strict", "--require-no-numbering",
                     "--dir", str(ws.root_dir))
    assert strict.code == 1, f"--require-no-numbering 未把标题序号纳入门禁:\n{strict.norm}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
