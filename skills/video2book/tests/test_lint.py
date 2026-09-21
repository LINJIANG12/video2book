# -*- coding: utf-8 -*-
"""交付物机器门禁：笔记成色规则内核、渲染合规规则内核，以及 `check --deliver` 的端到端口径。

迁自 `scripts/selfcheck.py` 的 `check_render_compat_rules` 与 `check_deliverable_lint_gate`。
原检查的后半段（扫开发机 `output/` 里真实产物、断言告警块与围栏配对为 0）依赖真实语料，
这里改写成「构造已知缺陷的样本 → 门禁必须拦下」的同口径版本：**提示词里喊的口号，
必须真的有机器判定兜底**，否则规则被改回去时没人知道。

规则内核是这一层的价值所在：`BOILERPLATE_PHRASES` / `HOLLOW_HEADINGS` /
`EPISODE_HEADING_PATTERNS` / `EPISODE_VOICE_PATTERNS` 五类致命项与
`_looks_truncated` / `STRUCTURE_KEYS` 两类警告项，每条规则都配一条「命中」和一条
「不误伤」。**不误伤那半尤其重要**——基准语料（微机原理 / 软件工程）在这五类上都是 0，
规则一宽就会把合格产物判成不合格，比漏判更伤。
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

from src.core.deliverable_lint import (  # noqa: E402
    BOILERPLATE_PHRASES,
    HOLLOW_HEADINGS,
    STRUCTURE_KEYS,
    check_note_structure,
    fatal_note_total,
    fatal_render_total,
    lint_note,
    lint_render,
    looks_like_stray_art,
    summarize_note,
    summarize_render,
)


def _doc(*blocks: str) -> str:
    return "\n\n".join(blocks) + "\n"


PLAIN_BODY = "数组在内存里连续存放，按下标访问的代价是常数时间。"


# ===========================================================================
# 一、lint_note 致命五类
# ===========================================================================

# ── 套话填充 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", BOILERPLATE_PHRASES)
def test_boilerplate_phrase_is_fatal(phrase: str):
    """黑名单里的无信息填充句必须被判致命：它们是那批上千行套话笔记的成因。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", f"{phrase}，这里补一句具体的技术说明。")
    hits = lint_note(text)["boilerplate"]
    assert [h["phrase"] for h in hits] == [phrase], f"套话未被判出: {phrase!r}"


def test_database_sample_boilerplate_is_caught():
    """最能代表那批坏样本的一句（同时写入模块笔记提示词黑名单）必须命中。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", "概念属性与边界：需要重点掌握。")
    assert len(lint_note(text)["boilerplate"]) == 2, "坏样本标志性套话漏判"


@pytest.mark.parametrize("body", [
    PLAIN_BODY,
    "核心考点的判定要看三个边界条件，而不是看名词出现的次数。",
    "这一节的判断标准很实在：能算出复杂度就写复杂度，算不出就别下结论。",
])
def test_informative_prose_is_not_boilerplate(body: str):
    """有具体信息的陈述不得被判套话（连『核心考点』这种词根相近的表述也要放过）。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", body)
    assert lint_note(text)["boilerplate"] == [], f"合格表述被误判为套话: {body!r}"


# ── 空壳容器标题 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", HOLLOW_HEADINGS)
def test_hollow_heading_is_fatal(name: str):
    """只起包装作用、不含信息的小节名必须被判致命（它们撑起了整篇笔记的骨架却没有内容）。"""
    text = _doc("# 测试笔记", f"## {name}", PLAIN_BODY)
    assert len(lint_note(text)["hollow_headings"]) == 1, f"空壳标题未被判出: {name!r}"


@pytest.mark.parametrize("heading", [
    "## 核心机制的实际运转代价",
    "## 难点梳理：三条判据",
    "## 横向对比的取舍",
    "## 知识拓扑的实际组织方式",
])
def test_specific_heading_is_not_hollow(heading: str):
    """把空壳名改写成带限定语的实义标题后必须放过——否则作者只能靠猜哪些词不能用。"""
    text = _doc("# 测试笔记", heading, PLAIN_BODY)
    assert lint_note(text)["hollow_headings"] == [], f"实义标题被误判为空壳: {heading!r}"


# ── 分集平铺标题 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("heading", [
    "## P01 绪论",                    # P 编号开口
    "### 第 3 讲 关系模型",            # 第 N 讲
    "## Part 2 索引结构",             # Part N
    "#### 1.1 第 2 讲 存储引擎",       # 手写序号 + 第 N 讲
    # 第二阶段 A3：正则曾写死 `^#{2,6}`，与「任何级别」的注释不符，一级标题整批漏网。
    "# P01 绪论",
    "# 第 1 讲 绪论",
    "# Part 2 索引",
])
def test_episode_flattened_heading_is_fatal(heading: str):
    """任何级别的标题都不得以分集编号 / 分集序号开口：那等于把笔记退化成课程录音的目录。"""
    text = _doc("# 测试笔记", heading, PLAIN_BODY)
    assert len(lint_note(text)["episode_headings"]) == 1, f"分集平铺标题漏判: {heading!r}"


@pytest.mark.parametrize("heading", [
    "## 3.2 版本元数据差异",           # 内容型编号（版本号）
    "## P 值与显著性判定",             # P 后面不是数字
    "#### 第 3 章 关系模型",           # 章不是讲/集/课/节
    "### 课程设计中的取舍",            # Part / P 都不出现
])
def test_content_heading_is_not_episode_flattened(heading: str):
    """长得像分集标题的内容型标题必须放过：基准语料里这类标题数以百计。"""
    text = _doc("# 测试笔记", heading, PLAIN_BODY)
    assert lint_note(text)["episode_headings"] == [], f"内容型标题被误判: {heading!r}"


# ── 行内残缺引用块 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bullet", [
    "- 辨析本质：> **易错点**：浮点数比较要用容差。",
    "- 操作提醒：> [!TIP] 先备份再动手。",
])
def test_inline_residual_quote_is_fatal(bullet: str):
    """列表项里残留 `>` 引用块会原样渲染出 `>` 与 `**`：必须判致命。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", bullet)
    assert len(lint_note(text)["inline_quote"]) == 1, f"行内残缺引用块漏判: {bullet!r}"


@pytest.mark.parametrize("line", [
    "- 当 n > 1 时进入循环分支，否则直接返回。",   # `>` 是比较运算符
    "> **易错点**：浮点数比较要用容差。",          # 正规引用块（整行）
    "- 引用来源见上方小节说明。",
])
def test_legitimate_greater_than_is_not_inline_quote(line: str):
    """比较运算符与正规引用块不得被判成残缺引用块——误判会逼作者不敢写 `>`。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", line)
    assert lint_note(text)["inline_quote"] == [], f"正常写法被误判: {line!r}"


# ── 分集口吻 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "上一讲提到的哈希冲突处理在这里会失效。",
    "本集主要讲解索引的物理结构。",
    "视频中说到内存对齐带来的额外开销。",
    "P03 里介绍了 B+ 树的分裂过程。",
])
def test_episode_voice_is_fatal(line: str):
    """正文以「本集 / 上一讲 / 视频中提到」叙述等于把笔记写成听课记录，必须判致命。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", line)
    assert len(lint_note(text)["episode_voice"]) == 1, f"分集口吻漏判: {line!r}"


@pytest.mark.parametrize("line", [
    "本课程共十二讲，覆盖全部存储引擎。",       # 「本课」后接「程」，不是口吻
    "先前的结论在这里被重新审视。",
    "讲师在板书里写下了三个边界条件。",         # 「讲师」后接「在」，不是口吻
])
def test_neutral_prose_is_not_episode_voice(line: str):
    """正常讲解不得被判分集口吻（『本课程』『讲师在』这类词根相近的表述尤其容易误伤）。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", line)
    assert lint_note(text)["episode_voice"] == [], f"正常讲解被误判为分集口吻: {line!r}"


def test_source_annotation_p_line_is_not_episode_voice():
    """`> 来源：P03 …` 是允许的来源标注形态，即使里面出现 `P03 里介绍` 也不判口吻。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", "> 来源：P03 里介绍了 B+ 树的分裂过程")
    assert lint_note(text)["episode_voice"] == [], "来源标注被误判为分集口吻"


def test_fenced_episode_voice_is_not_counted():
    """围栏内的内容不参与成色判定：代码块里转述讲师原话属正常写作。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", "```text\n上一讲提到的哈希冲突在这里失效。\n```")
    assert lint_note(text)["episode_voice"] == [], "围栏内的转述被计入分集口吻"


# ===========================================================================
# 二、lint_note 警告项：断句与结构完备性
# ===========================================================================

@pytest.mark.parametrize("body", [
    "先看数组：按下标访问的代价是常数时间，而插入与删除要搬移元素并重新分配，所以",   # 悬垂虚词
    "第一处代价是搬移元素，第二处代价是扩容时的停顿，第三处是缓存失效，",              # 逗号收尾
    "这里的判断标准是**先看访问模式，再看数据结构与内存布局的代价",                   # 加粗标记未配对
])
def test_truncated_tail_is_a_warning(body: str):
    """被砍断的长句必须能被测出来：它正是「上半句留在正文里、下半句丢了」的现场。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", body)
    assert len(lint_note(text)["truncated"]) == 1, f"断句漏判: {body!r}"


def test_short_dangling_fragment_is_not_truncated():
    """30 字以下一律不判：短句以虚词收尾是正常口语，判了会把标题与列表项全打成断句。"""
    assert lint_note(_doc("# 测试笔记", "## 数组的连续布局", "所以"))["truncated"] == []


def test_short_unbalanced_bold_is_not_truncated():
    """短行里未配对的 `**` 也不判——阈值就是 30 字。

    第二阶段 A5 删掉了 `_looks_truncated` 里恒真的 `len(text) >= 20`（它排在
    `if len(text) < 30: return False` 之后，永远为真）。行为不变，这里把真实阈值钉住，
    免得以后有人以为「短行也查加粗配对」而误改回去。
    """
    assert lint_note(_doc("# 测试笔记", "## 数组的连续布局", "- **未闭合"))["truncated"] == []


def test_long_unbalanced_bold_is_truncated():
    """长行里未配对的 `**` 是原文被砍断的典型特征（与上面那条成对，锚住阈值）。"""
    body = "- " + "内容" * 20 + " **未闭合"
    text = _doc("# 测试笔记", "## 数组的连续布局", body)
    assert len(lint_note(text)["truncated"]) == 1, "长行里未配对的加粗标记漏判"


@pytest.mark.parametrize("body", [
    "数组适合随机访问，代价是插入与删除要搬移元素。",               # 正常收尾
    "先看访问模式，再决定用哪个结构；两处代价都要写清楚。",
])
def test_complete_sentence_is_not_truncated(body: str):
    """完整句子不得被判断句，否则每份笔记都会挂满假警告。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", body)
    assert lint_note(text)["truncated"] == [], f"完整句子被误判为断句: {body!r}"


def test_table_row_ending_with_comma_is_not_truncated():
    """GFM 表格行以逗号结尾是排版常态，必须靠 `|` 前导豁免掉。"""
    row = "| 数组 | 常数时间访问 | 插入与删除要搬移元素，扩容还要重新分配， |"
    assert lint_note(_doc("# 测试笔记", "## 数组的连续布局", row))["truncated"] == []


def test_comma_tail_without_leading_bar_is_truncated():
    """前导 `|` 是豁免判据本身：同样的行去掉 `|` 就必须被测出来。"""
    body = "数组 | 常数时间访问 | 插入与删除要搬移元素，扩容还要重新分配，"
    text = _doc("# 测试笔记", "## 数组的连续布局", body)
    assert len(lint_note(text)["truncated"]) == 1, "去掉前导 `|` 后断句反而测不出来"


# ── 结构完备性 ──────────────────────────────────────────────────────────────

def test_structure_detects_missing_h1():
    assert check_note_structure(_doc("## 只有二级标题", PLAIN_BODY))["has_h1"] is False


def test_structure_detects_single_section():
    """只有一个小节不算分节：现行笔记版式要求至少两个知识主题小节。"""
    structure = check_note_structure(_doc("# 标题", "## 唯一小节", PLAIN_BODY))
    assert structure["has_sections"] is False


def test_structure_rejects_h5_and_h6():
    """标题层级最多到 `####`：再深的层级在笔记里只会让目录碎成一片。"""
    assert check_note_structure(_doc("# 标题", "##### 五级"))["no_h5plus_headings"] is False
    assert check_note_structure(_doc("# 标题", "###### 六级"))["no_h5plus_headings"] is False
    assert check_note_structure(_doc("# 标题", "#### 四级"))["no_h5plus_headings"] is True


def test_structure_rejects_numbered_headings():
    """手写序号与阅读器自动编号会叠成 `1. 1. 前端开发工具链`，结构项必须判出。"""
    assert check_note_structure(_doc("# 标题", "## 1. 数组的连续布局"))["no_numbered_headings"] is False
    assert check_note_structure(_doc("# 标题", "## 数组的连续布局"))["no_numbered_headings"] is True


def test_structure_skips_fenced_headings():
    """代码块里以 `#` 开头的注释不是标题，不得计入序号统计。"""
    text = _doc("# 标题", "## 数组的连续布局", "```bash\n# 1. 这不是标题\n```")
    assert check_note_structure(text)["no_numbered_headings"] is True


def test_structure_section_count_skips_fenced_headings():
    """围栏里的 `##` 不算小节。

    第二阶段 A4：`has_sections` 以前直接对全文 `re.findall(r"^##\\s+\\S", …)`，
    于是**围栏里的两个假标题就能把 `--require-structure` 骗过去**。
    """
    text = _doc("# 标题", "```text\n## 甲\n## 乙\n```")
    assert check_note_structure(text)["has_sections"] is False


def test_structure_h1_skips_fenced_headings():
    """围栏里的 `#` 不算 H1（同上，A4）。"""
    text = _doc("```text\n# 假的 H1\n```", "## 甲", PLAIN_BODY, "## 乙", PLAIN_BODY)
    structure = check_note_structure(text)
    assert structure["has_h1"] is False
    assert structure["has_sections"] is True


def test_structure_redundant_tail_skips_fenced_headings():
    """围栏里的 `## 速查卡` 是**示例代码**，不得触发「多余收尾小节」误报（A4）。"""
    text = _doc("# 标题", "## 数组的连续布局", "```text\n## 速查卡\n```", "## 复杂度取舍")
    assert check_note_structure(text)["no_redundant_tail"] is True


@pytest.mark.parametrize("tail", ["## 速查卡", "## 一句话总纲"])
def test_structure_rejects_redundant_tail(tail: str):
    """末尾多余的收尾小节与正文重复，现行版式已删除，必须判出。"""
    text = _doc("# 标题", "## 数组的连续布局", PLAIN_BODY, tail)
    assert check_note_structure(text)["no_redundant_tail"] is False


def test_structure_is_complete_for_current_layout():
    """符合现行版式的笔记五项结构必须全绿（否则警告项会永远亮着，等于没有）。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", PLAIN_BODY, "## 复杂度取舍", PLAIN_BODY)
    structure = check_note_structure(text)
    assert all(structure.get(key) for key in STRUCTURE_KEYS), f"结构判定异常: {structure}"


def test_summarize_note_counts_missing_structure():
    """`structure_missing` 是结构项的计数出口，必须按 STRUCTURE_KEYS 统计。"""
    structure = {key: True for key in STRUCTURE_KEYS}
    structure["no_h5plus_headings"] = False
    structure["no_redundant_tail"] = False
    summary = summarize_note({
        "boilerplate": [], "hollow_headings": [], "episode_headings": [],
        "inline_quote": [], "truncated": [], "episode_voice": [],
        "structure": structure,
    })
    assert summary["structure_missing"] == 2


def test_fatal_note_total_excludes_warning_findings():
    """断句与结构缺件只提示不门禁：连基准语料都无法归零，拿它当门禁只会全盘误杀。"""
    summary = {
        "boilerplate": 1, "hollow_headings": 1, "episode_headings": 1,
        "inline_quote": 1, "episode_voice": 1,
        "truncated": 7, "structure_missing": 3,
    }
    assert fatal_note_total(summary) == 5, "致命项口径漂移（断句 / 结构缺件混进了门禁）"


# ===========================================================================
# 三、lint_render 规则内核
# ===========================================================================

@pytest.mark.parametrize("kind", ["TIP", "NOTE", "WARNING", "IMPORTANT", "CAUTION"])
def test_github_alert_block_is_fatal(kind: str):
    """Typora 不渲染 GitHub 告警块，会原样露出 `> [!TIP]`：五种都要判致命。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", f"> [!{kind}]", "> 先备份再动手。")
    assert len(lint_render(text)["alert_blocks"]) == 1, f"{kind} 告警块漏判"


@pytest.mark.parametrize("line", [
    "> 提示：先备份再动手。",                        # 普通引用块
    "> 提示：[!TIP] 这种写法不成语法。",             # `>` 与 `[!KIND]` 之间夹了文字
])
def test_plain_quote_is_not_alert_block(line: str):
    """普通引用块不得被判成告警块：告警块语法要求 `>` 紧跟 `[!KIND]`，夹字就不算。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", line)
    assert lint_render(text)["alert_blocks"] == [], f"普通引用被误判: {line!r}"


def test_fenced_alert_block_is_not_counted():
    """围栏内的告警块是待讲解的代码示例，不算交付物的渲染缺陷。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", "```markdown\n> [!TIP]\n> 示例\n```")
    assert lint_render(text)["alert_blocks"] == [], "围栏内的告警块被计入"


@pytest.mark.parametrize("text, balanced", [
    ("```text\n代码\n```\n", True),
    ("```text\n代码\n", False),
    ("```text\n代码\n```\n\n```text\n又一段\n```\n", True),
    ("正文没有任何围栏。\n", True),
])
def test_fence_pairing_is_detected(text: str, balanced: bool):
    """围栏必须成对闭合：漏一个闭合符会把后面整篇正文吞进代码块。"""
    assert lint_render(text)["fences_unbalanced"] is (not balanced)


@pytest.mark.parametrize("text, expected", [
    ("```\n裸围栏\n```\n", 1),
    ("```text\n字符画\n```\n", 0),
    ("```python\nprint(1)\n```\n", 0),
])
def test_fence_without_lang_is_reported(text: str, expected: int):
    """开启围栏缺语言标识要报出来（默认提示项，`--require-lang` 才入门禁）。"""
    assert len(lint_render(text)["fence_without_lang"]) == expected


def test_closing_fence_is_not_counted_as_missing_lang():
    """裸闭合围栏不是缺陷：计数若把闭合行也算进去，健康的文档会永远挂着一堆假告警。"""
    lint = lint_render("```text\n字符画\n```\n")
    assert lint["fence_without_lang"] == [], "闭合围栏被当成缺语言标识的开启围栏"
    assert lint["fences_unbalanced"] is False


def test_fatal_render_total_excludes_missing_lang():
    """缺语言标识是提示项：致命总数只认告警块 / 裸图 / 围栏配对三类。"""
    summary = summarize_render(lint_render("```\n裸围栏\n```\n"))
    assert summary["fence_without_lang"] == 1
    assert fatal_render_total(summary) == 0, "缺语言标识混进了致命总数"


# ── 围栏外裸字符画 ──────────────────────────────────────────────────────────

def test_stray_art_outside_fence_is_fatal():
    """围栏外裸字符画会随字体走形：必须判致命。"""
    art = "┌────────────────────┐"
    text = _doc("# 测试笔记", "## 数组的连续布局", art, "└────────────────────┘")
    assert len(lint_render(text)["stray_art"]) == 2, "围栏外裸字符画漏判"


def test_stray_art_inside_fence_is_not_counted():
    """字符画进了围栏就是合规写法，不得再判。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", "```text\n┌────────────┐\n└────────────┘\n```")
    assert lint_render(text)["stray_art"] == [], "围栏内的字符画被误判为裸图"


def test_table_row_with_box_chars_is_not_stray_art():
    """表格行以 `|` 开头即豁免：`| ──── |` 是表格分隔，不是字符画。"""
    text = _doc("# 测试笔记", "## 数组的连续布局", "| ──── | ──── |")
    assert lint_render(text)["stray_art"] == [], "表格行被误判为裸字符画"


@pytest.mark.parametrize("line, expected", [
    ("┌────────────────────┐", True),                       # 纯框图
    ("│ 客户端 │ ──────> │ 服务端 │", True),                  # 框线 + 连线
    ("客户端   -->   服务端   -->   数据库", True),            # 空格对齐 + 箭头连线
    ("信号地 GND ──── 直通连接信号地 GND", False),             # 破折号散文（密度 0.20 < 0.30）
    ("用 `┌`、`─`、`┐` 说明框线字符", False),                  # 行内代码豁免
    ("─ ─ ─ 只有三个框线符", False),                           # 密度达标但无连续框线串、不足 6 个
    ("普通正文一行，没有任何特殊字符。", False),
])
def test_looks_like_stray_art(line: str, expected: bool):
    """密度阈值 0.30 + 连续框线串双重约束：一条防破折号散文，一条防零散制表符。"""
    assert looks_like_stray_art(line) is expected, f"裸图判定异常: {line!r}"


# ===========================================================================
# 四、迁自 check_render_compat_rules：共享规则不得被追加两遍
# ===========================================================================

def test_synthesis_prompt_does_not_append_render_compat_rules():
    """笔记提示词不得再追加 RENDER_COMPAT_RULES：【排版】一节已覆盖同一批要求，
    说两遍只会稀释重点（笔记只有一种风格，没有任何分支需要补丁）。"""
    from src.generator.block_synthesizer import BlockSynthesizer
    from src.prompts import RENDER_COMPAT_RULES

    block_meta = {"block_id": 1, "block_title": "t", "episodes": [1], "core_theme": "x"}
    rendered = BlockSynthesizer.build_synthesis_prompt(block_meta, [])
    assert RENDER_COMPAT_RULES not in rendered, "笔记提示词又把渲染硬约束追加了一遍"


def test_synthesis_prompt_is_built_without_style_variants():
    """八种旧笔记风格已删除：风格参数一旦回来，渲染硬约束就会重新长出分支。"""
    import inspect

    from src.generator.block_synthesizer import BlockSynthesizer

    params = inspect.signature(BlockSynthesizer.build_synthesis_prompt).parameters
    assert "style" not in params, "build_synthesis_prompt 不应再有 style 参数"


# ===========================================================================
# 五、迁自 check_deliverable_lint_gate：`check --deliver` 端到端口径
# ===========================================================================

CLEAN_NOTE = (
    "# 数据结构与算法笔记\n\n"
    "## 数组的连续布局\n\n"
    "数组在内存里连续存放，按下标访问的代价是常数时间；链表的节点分散在堆上，插入只需改两处指针。\n\n"
    "- 数组适合随机访问，代价是插入与删除要搬移元素。\n"
    "- 链表适合频繁插入，代价是无法按下标直接定位。\n\n"
    "## 复杂度取舍\n\n"
    "哈希表把平均查找压到常数时间，代价是冲突处理与扩容时的停顿。先看访问模式，再决定用哪个结构。\n\n"
    "## 边界条件的检查\n\n"
    "循环不变式要在进入循环前成立、每轮结束后保持。空输入与单元素输入是最容易漏掉的两处。\n"
)

# 三类渲染致命项各一段样本：门禁必须逐类拦下
RENDER_FATAL_SUFFIXES = {
    "alert_block": "\n> [!TIP]\n> 先备份再动手。\n",
    "unbalanced_fence": "\n```text\n字符画进了围栏却没有闭合\n",
    "stray_art": "\n┌────────────────────┐\n└────────────────────┘\n",
}

MISSING_LANG_SUFFIX = "\n```\n裸围栏没有语言标识\n```\n"


def _deliver_note(make_workspace, name: str, body: str):
    ws = make_workspace(name)
    (ws.notes_dir / "笔记01_数组与链表_笔记.md").write_text(pad_to(body), encoding="utf-8")
    return ws


def test_deliver_strict_passes_on_clean_note(run_cli, make_workspace):
    """合格产物在 `--strict` 下必须放行；误杀合格产物比漏判更伤。"""
    ws = _deliver_note(make_workspace, "门禁_合格", CLEAN_NOTE)
    result = run_cli("check", "--deliver", "--strict", "--dir", str(ws.root_dir))
    assert result.code == 0, f"合格笔记被门禁拦下:\n{result.norm}"
    assert "[OK]" in result.norm, "未给出通过结论"


@pytest.mark.parametrize("kind", sorted(RENDER_FATAL_SUFFIXES))
def test_deliver_strict_fails_on_render_fatal(run_cli, make_workspace, kind: str):
    """端到端：告警块 / 围栏未闭合 / 围栏外裸字符画任一类出现，`--strict` 必须非零退出。"""
    ws = _deliver_note(make_workspace, f"门禁_{kind}", CLEAN_NOTE + RENDER_FATAL_SUFFIXES[kind])
    result = run_cli("check", "--deliver", "--strict", "--dir", str(ws.root_dir))
    assert result.code == 1, f"{kind} 未被门禁拦下:\n{result.norm}"
    assert "[FAIL]" in result.norm, "未给出失败原因"


def test_deliver_fatal_note_craft_is_gated(run_cli, make_workspace):
    """端到端：笔记成色致命项（套话 + 空壳标题）必须让 `--strict` 非空退出。"""
    dirty = CLEAN_NOTE + "\n## 重点难点梳理\n\n概念属性与边界：该知识点非常重要。\n"
    ws = _deliver_note(make_workspace, "门禁_成色致命", dirty)
    result = run_cli("check", "--deliver", "--strict", "--dir", str(ws.root_dir))
    assert result.code == 1, f"成色致命项未被拦下:\n{result.norm}"


def test_deliver_require_lang_turns_missing_lang_into_a_gate(run_cli, make_workspace):
    """缺语言标识默认只提示；`--require-lang` 一加上就必须真拦（否则那个开关是摆设）。"""
    ws = _deliver_note(make_workspace, "门禁_语言标识", CLEAN_NOTE + MISSING_LANG_SUFFIX)
    assert run_cli("check", "--deliver", "--strict", "--dir", str(ws.root_dir)).code == 0, \
        "缺语言标识默认是提示项，不该让 --strict 失败"
    strict = run_cli("check", "--deliver", "--strict", "--require-lang",
                     "--dir", str(ws.root_dir))
    assert strict.code == 1, f"--require-lang 未把缺语言标识纳入门禁:\n{strict.norm}"


def test_deliver_gate_reports_zero_findings_on_clean_workspace(run_cli, make_workspace):
    """JSON 口径是脚本化巡检的判据：合格产物上各项计数必须全为 0。"""
    ws = _deliver_note(make_workspace, "门禁_JSON", CLEAN_NOTE)
    result = run_cli("check", "--deliver", "--strict", "--json", "--dir", str(ws.root_dir))
    assert result.code == 0, result.norm
    payload = json.loads(result.out)
    render = payload["render"][0]["totals"]
    assert render["alert_blocks"] == 0 and render["stray_art"] == 0
    assert render["fences_unbalanced"] == 0 and render["fence_without_lang"] == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
