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
from typing import List

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import pad_to  # noqa: E402

from src.core import heading_cleanup  # noqa: E402
from src.core.deliverable_lint import lint_heading_numbers  # noqa: E402
from src.core.heading_numbers import (  # noqa: E402
    is_numbered_heading,
    plan_heading_cleanup,
    strip_heading_number,
)

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


# ---------------------------------------------------------------------------
# 文档级判定：同一形态在不同文档里结论不同
# ---------------------------------------------------------------------------
# 实测教训：行级形态分不出「该剥」与「不该剥」，因为
#   `## 2 类属性`（该剥，作者原意）与 `### 4 厘米的钥匙扣案`（不该剥）
# 形态完全一致。换轴到「是否成序」之后两者才分得开。

# 必须**自动剥**：(整篇文本, 期望被剥掉的行)
AUTO_CASES = (
    # 成序的题号——但剥掉会剩 `题：…` 残句，所以其实进报告档（见 REPORT_CASES）
    # 这里是干净的成序数字 + 汉字：作者原本要清的「数字 + 汉字」标题族
    ("# 篇\n\n## 1 概念\n\n## 2 类属性\n\n## 3 方法\n", ["## 概念", "## 类属性", "## 方法"]),
    # 分隔符形态是强证据，不要求成序
    ("# 篇\n\n## 1. 列表标签的三大分类\n", ["## 列表标签的三大分类"]),
    ("# 篇\n\n### 2.1 无序列表的语义\n", ["### 无序列表的语义"]),
    ("# 篇\n\n## 3、工程定位\n", ["## 工程定位"]),
    # 章号前缀带边界
    ("# 篇\n\n## 第 3 章：关系模型\n", ["## 关系模型"]),
    # 混用形态时按数值成序（1.1→1、2.1→2 同属 1/2 段）
    ("# 篇\n\n#### 1.1 年月日与时分秒的独立获取\n\n### 2.1 无序列表\n",
     ["#### 年月日与时分秒的独立获取", "### 无序列表"]),
)

# 必须**原样保留**：数字是内容，剥掉就是永久损坏。这些是实测踩到的原样。
KEEP_CASES = (
    # 计量单位：数字是量
    "## 80 小时和 60 小时的两道坎",
    "### 4 厘米的钥匙扣案",
    # 孤立数字：全篇没有第二个同类标题，无从成序
    "### 520 沟通题",
    "## 315 多省联考判断推理逐题精讲",
    # 章号后无边界：`第14讲` 的 `的` 说明它不是「第 14 讲：」这种引用
    "## 第14讲的收尾：量刑情节的完整地图",
)

# 必须进**报告档**（只报不改）并给出理由：(整篇文本, 期望出现在 report 里的片段)
REPORT_CASES = (
    # 孤立 + 量词
    ("# 篇\n\n## 80 小时和 60 小时的两道坎\n", "计量单位"),
    ("# 篇\n\n### 4 厘米的钥匙扣案\n", "计量单位"),
    # 孤立，无护栏
    ("# 篇\n\n### 520 沟通题\n", "孤立数字"),
    ("# 篇\n\n## 315 多省联考判断推理逐题精讲\n", "孤立数字"),
    # 成序但剥完是残句：应建议把编号移到末尾，而不是删号
    ("# 篇\n\n## 71 题：先画个九宫格\n\n## 72 题：看对称\n", "残句"),
    # 残句判定必须认中缀点：实测 `### 100 题 · 食用果蔬…` 会被剥成 `### 题 · …`
    ("# 篇\n\n### 100 题 · 食用果蔬与老年人认知能力\n", "残句"),
    ("# 篇\n\n### 75、76题：越简单，越容易想复杂\n", "分隔符后接数字"),
    # 计数词/连词护栏
    ("# 篇\n\n## 3 种方案的取舍\n", "计数词"),
    ("# 篇\n\n#### 5.7 与 8.0 版本元数据呈现差异\n", "计数词"),
)


@pytest.mark.parametrize("text, expected", AUTO_CASES)
def test_document_level_auto_strip(text: str, expected: List[str]):
    """成序标题必须被自动剥掉，且只动序号、不动标题文字。"""
    plan = plan_heading_cleanup(text)
    got = [item["after"] for item in plan["auto"]]
    assert got == expected, f"自动剥结果不符：{got}"
    assert plan["report"] == [], f"不该有报告项：{plan['report']}"


@pytest.mark.parametrize("line", KEEP_CASES)
def test_document_level_keeps_content_numbers(line: str):
    """孤立/带单位的数字**一个都不许自动剥**——这些是实测被削掉过头的标题。"""
    text = f"# 篇\n\n{line}\n"
    plan = plan_heading_cleanup(text)
    assert plan["auto"] == [], f"内容型标题被自动剥: {plan['auto']}"


@pytest.mark.parametrize("text, needle", REPORT_CASES)
def test_document_level_reports_and_explains(text: str, needle: str):
    """低置信项必须进报告档（不写盘）并带上理由，供人眼一次确认。"""
    plan = plan_heading_cleanup(text)
    assert plan["auto"] == [], f"低置信项被自动剥了: {plan['auto']}"
    assert plan["report"], "低置信项既没剥也没报，等于被静默放过"
    for item in plan["report"]:
        assert needle in item["reason"], f"理由未命中 {needle!r}: {item['reason']}"
        assert item["hint"], "报告项必须给出处置建议"


def test_report_tier_does_not_reach_disk_via_clean_text():
    """报告档的东西绝不落盘：`clean_text` 只能改 auto 档。"""
    text = "# 篇\n\n## 80 小时和 60 小时的两道坎\n\n## 315 多省联考判断推理逐题精讲\n"
    new_text, headings, _toc, _changes, suspects = heading_cleanup.clean_text(text)
    assert new_text == text and headings == 0, "报告档被写进了正文"
    assert len(suspects) == 2, f"报告档未进 suspects: {suspects}"


def test_document_level_offers_actionable_hint_for_question_headings():
    """题号类给的是「把编号移到末尾」的可执行建议，不是删号。"""
    plan = plan_heading_cleanup("# 篇\n\n## 71 题：先画个九宫格\n\n## 72 题：看对称\n")
    hints = [item["hint"] for item in plan["report"]]
    assert any("先画个九宫格（第 71 题）" in h for h in hints), f"建议不可执行: {hints}"


def test_chapter_prefix_requires_boundary():
    """章号后必须是分隔符/空白/结尾：`第14讲的收尾` 里的 `第14讲` 是句子成分，不是章号。"""
    assert strip_heading_number("## 第 3 章：关系模型") == "## 关系模型"
    assert strip_heading_number("## 第14讲的收尾：量刑情节的完整地图") == "## 第14讲的收尾：量刑情节的完整地图"


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


def test_check_fix_numbering_default_is_report_only(run_cli, make_workspace):
    """端到端：缺省**只报不改**。去号判定含猜的成份，猜错一次就是内容永久损坏，
    所以写盘必须显式 `--apply`——这条默认行为本身就是安全护栏。"""
    ws = make_workspace("清理_默认只报")
    article = ws.articles_dir / "模块01_绪论与数制_精读长文.md"
    article.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    result = run_cli("check", "--fix-numbering", "--dir", str(ws.root_dir), "--json")
    assert result.code == 0, result.norm
    payload = json.loads(result.out)
    assert payload["dry_run"] is True and payload["applied"] is False
    assert payload["totals"]["headings"] == 2, f"未统计出待改行数: {payload['totals']}"
    assert article.read_text(encoding="utf-8") == NUMBERED_ARTICLE, "缺省却写盘了"


def test_check_fix_numbering_applies_in_place(run_cli, make_workspace):
    """端到端：`--fix-numbering --apply` 才就地改掉存量产物里的标题序号。"""
    ws = make_workspace("清理_就地去号")
    article = ws.articles_dir / "模块01_绪论与数制_精读长文.md"
    article.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    result = run_cli("check", "--fix-numbering", "--apply", "--dir", str(ws.root_dir), "--json")
    assert result.code == 0, result.norm
    payload = json.loads(result.out)
    assert payload["applied"] is True
    totals = payload["totals"]
    assert totals["headings"] == 2 and totals["changed_files"] == 1, f"清理报告异常: {totals}"

    text = article.read_text(encoding="utf-8")
    assert "## 1." not in text and "### 2.1" not in text, "标题序号没被就地剥掉"
    assert "正文一。" in text and "正文二。" in text, "正文被改动"


def test_check_fix_numbering_is_idempotent(run_cli, make_workspace):
    """端到端：清理过的产物再跑一遍必须零改动 —— 这是它可以被反复调度的前提。"""
    ws = make_workspace("清理_幂等")
    article = ws.articles_dir / "模块01_绪论与数制_精读长文.md"
    article.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    assert run_cli("check", "--fix-numbering", "--apply", "--dir", str(ws.root_dir)).code == 0
    after_first = article.read_text(encoding="utf-8")

    second = run_cli("check", "--fix-numbering", "--apply", "--dir", str(ws.root_dir), "--json")
    assert second.code == 0, second.norm
    totals = json.loads(second.out)["totals"]
    assert totals["changed_files"] == 0 and totals["headings"] == 0, f"第二轮仍有改动: {totals}"
    assert article.read_text(encoding="utf-8") == after_first, "第二轮改动或改写了文件"


def test_check_fix_numbering_skips_task_files(run_cli, make_workspace):
    """任务书不清理：它是派发物，改了名字与内容会让在途任务凭空错位。"""
    ws = make_workspace("清理_跳过任务书")
    task = ws.articles_dir / "模块01_绪论与数制_TASK.md"
    task.write_text(NUMBERED_ARTICLE, encoding="utf-8")

    result = run_cli("check", "--fix-numbering", "--apply", "--dir", str(ws.root_dir), "--json")
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
