# -*- coding: utf-8 -*-
"""标题序号纪律：判定与剥离「手写序号」（笔记 / 长文 / 教材共用同一处规则）。

背景（实测踩过两轮）：交付物默认在 Typora 等阅读器中阅读，它们会**自动**给标题编号。
标题里再手写一套序号（笔记的 `## 1. …`、教材的 `## 第 3 章：…` 与继承来的 `## 2.1 …`）
就会与自动编号叠在一起显示两遍。所以现行口径是：**标题一律不写序号**，序号交给阅读器。
长文提示词与教材整编因此都不再写号，并常驻一个「幂等去号」步骤把存量产物清干净。

剥离规则（幂等；只动标题行，正文一字不碰；跳过代码围栏）：
1. 先整体吃下数字 token：`1` / `2.1` / `12.3.4`（**贪心吃满**，否则 `2.1` 会被误剥成 `1`），
   且只认 **1~3 位**的数字——4 位数（`2025 前端开发的技术演进`、`1963 年火星火箭`、`1418 报错根源`）
   是年份 / 错误码等**内容**，一律保留；
2. 后随标点分隔（`1. `、`2.1 `、`3、`）→ 剥；但**分隔符后面紧跟数字**时不剥
   （`0、1 与 NULL 的三值逻辑闭包` 里的 `0、1` 是内容）；
3. 后随空白 → 剥；跟随「内容信号」时不剥——计数词（`3 种方案的取舍`、`1.5 倍速`）
   与并列连词（`5.7 与 8.0 版本元数据呈现差异`）。这个例外清单刻意收得极窄：实测六门课语料里
   `## 2 类属性`、`### 7 小结`、`### 1 组包与解包的含义`、`#### 3.7 点击 Install 并等待`
   这类「数字 + 汉字」标题**几乎都是章节号**，宽清单只会留下一堆双重编号；
4. 章号前缀（`第 3 章：`、`第 12 讲`）一律剥。
"""

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

HEADING_RE = re.compile(r"^(#{1,6})([ \t]+)(.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# `第 3 章：` / `第 12 讲、` 这类章号前缀
CHAPTER_PREFIX_RE = re.compile(r"^第\s*\d{1,4}\s*[章讲节]\s*[：:、.．\-]?\s*")
# 数字 token：贪心吃满小数点分段——`2.1` 必须整体吃掉，否则会退化成只吃 `2.`。
# 只认 1~3 位：4 位数是年份 / 错误码（`2025 前端开发…`、`1418 报错根源`）等**内容**，不是章节号。
NUMBER_TOKEN_RE = re.compile(r"^\d{1,3}(?:\.\d+)*")
# 「内容信号」开头：数字紧跟这些词时说明数字是内容而非编号，要保留。
#   计数词：`3 种方案`、`1.5 倍速`
#   并列连词：`5.7 与 8.0 版本元数据呈现差异`
# 刻意收得极窄——实测把 类/个/项/层/段/组/列/大/小/点/年… 都算进来之后，
# 六门课里那上百处「数字 + 汉字」标题几乎全是章节号，护栏越宽留下的双重编号越多。
CONTENT_HEAD_RE = re.compile(r"^(种|倍|与|和|及|或|至|到)")
# 标点分隔符：`1.` / `1、` / `1．`
SEPARATOR_CHARS = ".、．"


def _strip_body(body: str) -> str:
    """剥掉标题正文的序号前缀；无可剥时原样返回。"""
    if not body:
        return body

    stripped = CHAPTER_PREFIX_RE.sub("", body, count=1)
    if stripped != body:
        return stripped if stripped.strip() else body

    match = NUMBER_TOKEN_RE.match(body)
    if not match:
        return body
    rest = body[match.end():]
    if not rest:
        return body

    if rest[0] in SEPARATOR_CHARS:
        after = rest[1:].lstrip()
        if not after.strip():
            return body
        if after[0].isdigit():
            return body      # `0、1 与 NULL…`：分隔符后面还是数字 → 数字本身是内容
        return after

    if rest[0] in " \t":
        after = rest.lstrip()
        if after and not CONTENT_HEAD_RE.match(after):
            return after
    return body


def strip_heading_number(line: str) -> str:
    """剥掉标题行的序号前缀（幂等）；非标题行或无需剥离时原样返回。"""
    match = HEADING_RE.match(line)
    if not match:
        return line
    body = match.group(3)
    stripped = _strip_body(body)
    if stripped == body:
        return line
    return f"{match.group(1)}{match.group(2)}{stripped}"


def is_numbered_heading(line: str) -> bool:
    """该行是否为「带手写序号」的标题（与 strip 同源判定，供门禁统计使用）。"""
    return strip_heading_number(line) != line


def iter_lines(text: str) -> Iterator[Tuple[int, str, bool]]:
    """逐行产出 `(行号, 原文, 是否在围栏内)`；围栏行本身记为「在围栏内」。"""
    in_fence = False
    for idx, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            yield idx, line, True
            in_fence = not in_fence
            continue
        yield idx, line, in_fence


def collect_numbered_headings(text: str) -> List[str]:
    """返回所有带手写序号的标题行（跳过围栏），供体检与清理复用。"""
    hits: List[str] = []
    for _idx, line, in_fence in iter_lines(text):
        if not in_fence and is_numbered_heading(line):
            hits.append(line.strip())
    return hits


def strip_text_heading_numbers(text: str) -> Tuple[str, int, List[Dict[str, Any]]]:
    """整体去号：返回 `(新文本, 改动条数, 改动明细)`；已无序号时改动数为 0（幂等）。"""
    changed: List[Dict[str, Any]] = []
    out: List[str] = []
    for line_no, line, in_fence in iter_lines(text):
        if not in_fence:
            new_line = strip_heading_number(line)
            if new_line != line:
                changed.append({"line": line_no, "before": line.strip(), "after": new_line.strip()})
                out.append(new_line)
                continue
        out.append(line)
    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, len(changed), changed


def first_bare_prefix(body: str) -> Optional[str]:
    """若正文以「数字 + 内容信号」开头（会被规则**保留**的可疑形态），返回该数字，否则 None。

    仅用于清理脚本的 `--dry-run` 报告：把这少数几行单独列出来，供人眼确认确实不该剥。
    """
    match = NUMBER_TOKEN_RE.match(body)
    if not match:
        return None
    token = match.group(0)
    rest = body[match.end():]
    if not rest:
        return None
    if rest[0] in SEPARATOR_CHARS:
        after = rest[1:].lstrip()
        return token if after[:1].isdigit() else None
    if rest[0] in (" ", "\t") and CONTENT_HEAD_RE.match(rest.lstrip()):
        return token
    return None
