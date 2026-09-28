# -*- coding: utf-8 -*-
"""标题序号纪律：判定与剥离「手写序号」（笔记 / 长文 / 教材共用同一处规则）。

背景（实测踩过两轮）：交付物默认在 Typora 等阅读器中阅读，它们会**自动**给标题编号。
标题里再手写一套序号（笔记的 `## 1. …`、教材的 `## 第 3 章：…` 与继承来的 `## 2.1 …`）
就会与自动编号叠在一起显示两遍。所以现行口径是：**标题一律不写序号**，序号交给阅读器。

## 为什么有「行级」与「文档级」两层

实测发现：`## 2 类属性`（该剥，作者原意）与 `### 4 厘米的钥匙扣案`（不该剥，数字是量词）
**形态完全一致**——都是「数字 + 空白 + 汉字」。行级形态规则无法区分，于是：

- **行级**（`strip_heading_number` / `is_numbered_heading`）：只判「形态像不像数字前缀、
  有没有被内容护栏救下」。它是**底层原语**，不是判定入口。
- **文档级**（`plan_heading_cleanup`）：新增「**成序**」这一轴——`71/72/73 题` 构成长度 ≥2、
  公差为 1 的连续段，说明数字是编号；`80 小时` 孤例无段可成，说明数字是量。
  **只有这里的结果才用来决定剥与不剥。**

`## 2 类属性` 之所以仍会被剥，是因为它身在 `1/2/3…` 的成序段里——作者原本要清的
「数字 + 汉字」标题族因此保住；而 `80 小时`、`4 厘米`、`520 沟通题`、`315 多省联考`
这类孤例不再被误伤。

## 判定详情

行级形态（`_bare_prefix` 的两条分支）：

1. **分隔符形态**（`1. `、`2.1 `、`3、`）：数字 token 后随标点分隔（`.`、`、`、`．`）。
   分隔符本身就是强证据，**不要求成序**。护栏：分隔符后紧跟数字时不剥
   （`0、1 与 NULL 的三值逻辑闭包` 里的 `0、1` 是内容）。
2. **空白形态**（`2 类属性`、`80 小时`）：数字 token 后随空白。弱证据，**要求成序**。
   护栏：数字后紧跟计数词（`3 种方案`、`1.5 倍速`）或计量单位（`4 厘米`、`80 小时`）时不剥。
   计数词清单刻意收得极窄——实测把 类/个/项/层/段/组/列 都算进来之后，
   六门课里那上百处「数字 + 汉字」标题几乎全是章节号，护栏越宽留下的双重编号越多。

数字 token 只认 **1~3 位**：4 位数（`2025 前端开发的技术演进`、`1963 年火星火箭`）
是年份 / 错误码等**内容**，一律保留。

章号前缀（`第 3 章：`、`第 12 讲`）一律剥，但**必须带边界**——`第14讲的收尾`
里的 `第14讲` 后面直接跟 `的`，说明它是句子成分而非章号引用，不剥。

## 分档

- **auto**：形态与成序都过关 → 可直接剥。
- **report**：形态像序号但被护栏救下、或孤立不成序、或剥完会剩残句
  （`## 71 题：…` → `## 题：…`）→ **只报不改**，附理由与建议，交人眼。
"""

import re
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

HEADING_RE = re.compile(r"^(#{1,6})([ \t]+)(.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# `第 3 章：` / `第 12 讲、` 这类章号前缀。
# 末尾的 `(?![^\s：:、.．\-])` 是边界断言：`[章讲节]` 后面必须已经是分隔符、空白或结尾。
# 没有它，`## 第14讲的收尾：…` 会被 `sub` 成 `## 的收尾：…`（实测踩过）。
CHAPTER_PREFIX_RE = re.compile(
    r"^第\s*\d{1,4}\s*[章讲节](?![^\s：:、.．\-])\s*[：:、.．\-]?\s*"
)
# 数字 token：贪心吃满小数点分段——`2.1` 必须整体吃掉，否则会退化成只吃 `2.`。
# 只认 1~3 位：4 位数是年份 / 错误码（`2025 前端开发…`、`1418 报错根源`）等**内容**，不是章节号。
NUMBER_TOKEN_RE = re.compile(r"^\d{1,3}(?:\.\d+)*")
# 「内容信号」开头：数字紧跟这些词时说明数字是内容而非编号，要保留。
#   计数词：`3 种方案`、`1.5 倍速`
#   并列连词：`5.7 与 8.0 版本元数据呈现差异`
# 刻意收得极窄——实测把 类/个/项/层/段/组/列/大/小/点/年… 都算进来之后，
# 六门课里那上百处「数字 + 汉字」标题几乎全是章节号，护栏越宽留下的双重编号越多。
CONTENT_HEAD_RE = re.compile(r"^(种|倍|与|和|及|或|至|到)")
# 计量单位：数字是**量**不是编号（`4 厘米的钥匙扣案`、`80 小时和 60 小时的两道坎`）。
# 清单窄，**只收多字单位**：单字的 `年/月/日/人/次/页/字` 会误救 `年月日与时分秒的独立获取`
# 这类标题（实测：`#### 1.1 年月日与时分秒的独立获取` 必须能剥），代价远大于收益。
# 判断方向也决定了取舍：漏掉单位的代价是「多剥一个」，而多收单位的代价是「该剥的剥不掉」，
# 后者会留下一堆双重编号——作者原本就是被这个逼到把清单收窄的。
MEASURE_WORDS = (
    "厘米", "毫米", "千米", "公里", "公斤", "千克", "吨",
    "小时", "分钟", "秒钟", "万元", "亿元", "岁",
)
# 标点分隔符：`1.` / `1、` / `1．`
SEPARATOR_CHARS = ".、．"
# 剥完会剩残句的表头词：数字是它修饰的名词的一部分（`71 题` 的 `71` 是题号，
# 但剥掉只剩 `题：…`）。分隔符收得宽——实测 `### 100 题 · 食用果蔬与老年人认知能力`
# 用的是中缀点 `·` 而不是冒号，只认冒号会漏判成可剥，落盘就是 `### 题 · …`。
DANGLING_HEAD_RE = re.compile(r"^(题|问|例|表|图|式|步|讲|节|章)\s*[：:、·・|｜—–\-,，。]")

# 成序段的判据：公差 1、长度至少这么长。取 2 是因为 `## 1 概念 / ## 2 类属性`
# 两节就是最常见的最小成序形态。
RUN_MIN_LENGTH = 2


class BarePrefix(NamedTuple):
    """一个标题的「裸数字前缀」及其形态。"""

    kind: str          # "sep"（数字 + 标点）或 "space"（数字 + 空白）
    token: str         # 原始数字 token，如 `2.1`
    value: int         # 首段数值，如 `2.1` → 2；成序段按它比较
    after: str         # 数字（含分隔符）之后的标题正文
    guarded: bool      # 是否被内容护栏救下（计数词 / 计量单位 / 分隔符后接数字）
    guard_reason: str  # 护栏说明，进报告用
    strong: bool       # 是否**无需成序**即可判定（分隔符形态，或多级小号如 `2.1`）


def _bare_prefix(body: str) -> Optional[BarePrefix]:
    """识别标题正文开头的「裸数字前缀」；无前缀或不构成前缀形态时返回 None。"""
    if not body:
        return None
    match = NUMBER_TOKEN_RE.match(body)
    if not match:
        return None
    token = match.group(0)
    rest = body[match.end():]
    if not rest:
        return None
    value = int(token.split(".")[0])
    # 多级小号（`2.1`、`3.7`）本身就是强证据：没人把 `2.1` 写成内容。
    # 它在文本里是「数字 + 空白」形态（token 已把 `.1` 一起吃掉了），所以要单独标出来，
    # 否则会被当成弱证据要求成序，而 `2.1` 常以孤例出现（如某节只有一个小号）。
    dotted = "." in token

    if rest[0] in SEPARATOR_CHARS:
        after = rest[1:].lstrip()
        if not after.strip():
            return None
        if after[0].isdigit():
            # `0、1 与 NULL…`：分隔符后面还是数字 → 数字本身是内容
            return BarePrefix("sep", token, value, after, True, "分隔符后接数字（多值并列）", False)
        return BarePrefix("sep", token, value, after, False, "", True)

    if rest[0] in " \t":
        after = rest.lstrip()
        if not after:
            return None
        if CONTENT_HEAD_RE.match(after):
            return BarePrefix("space", token, value, after, True, "数字后紧跟计数词/连词", False)
        if after.startswith(MEASURE_WORDS):
            return BarePrefix("space", token, value, after, True, "数字后紧跟计量单位", False)
        return BarePrefix("space", token, value, after, False, "", dotted)

    return None


def _run_values(values: Sequence[int]) -> set:
    """返回参与「公差 1、长度 ≥ RUN_MIN_LENGTH」连续段的数值集合。

    这是判定的换轴点：`71/72/73` 成段 → 数字是编号；`80/60` 不成段 → 数字是量。
    """
    ordered = sorted(set(values))
    caught: set = set()
    start = 0
    for index in range(1, len(ordered) + 1):
        if index < len(ordered) and ordered[index] == ordered[index - 1] + 1:
            continue
        if index - start >= RUN_MIN_LENGTH:
            caught.update(ordered[start:index])
        start = index
    return caught


def _strip_body(body: str) -> str:
    """剥掉标题正文的序号前缀；无可剥时原样返回（行级形态判定，无文档上下文）。"""
    if not body:
        return body

    stripped = CHAPTER_PREFIX_RE.sub("", body, count=1)
    if stripped != body:
        return stripped if stripped.strip() else body

    info = _bare_prefix(body)
    if info is None or info.guarded:
        return body
    return info.after


def strip_heading_number(line: str) -> str:
    """剥掉标题行的序号前缀（幂等）；非标题行或无需剥离时原样返回。

    这是**行级原语**：它只看这一行的形态，不判「成序」。判定入口是
    `plan_heading_cleanup`——它拿到整篇文本后才决定哪些行真的该剥。
    """
    match = HEADING_RE.match(line)
    if not match:
        return line
    body = match.group(3)
    stripped = _strip_body(body)
    if stripped == body:
        return line
    return f"{match.group(1)}{match.group(2)}{stripped}"


def is_numbered_heading(line: str) -> bool:
    """该行**形态上**是否像带手写序号的标题（行级；不含成序判定）。"""
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


def _rewrite(line: str, after: str) -> str:
    match = HEADING_RE.match(line)
    if not match:
        return line
    return f"{match.group(1)}{match.group(2)}{after}"


def plan_heading_cleanup(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """文档级去号计划：返回 `{"auto": [...], "report": [...]}`（跳过代码围栏）。

    - `auto`：可直接剥的行，含 `line` / `before` / `after`。
    - `report`：**只报不改**的行，含 `line` / `before` / `number` / `reason` / `hint`。

    判定顺序：章号前缀 → 行级形态与护栏 → 空白形态要求成序 → 残句护栏。
    """
    headings: List[Tuple[int, str, str]] = []
    for line_no, line, in_fence in iter_lines(text):
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            headings.append((line_no, line, match.group(3)))

    parsed: List[Tuple[int, str, str, BarePrefix]] = []
    chapter_lines: List[Tuple[int, str, str]] = []
    for line_no, line, body in headings:
        stripped = CHAPTER_PREFIX_RE.sub("", body, count=1)
        if stripped != body and stripped.strip():
            chapter_lines.append((line_no, line, stripped))
            continue
        info = _bare_prefix(body)
        if info is not None:
            parsed.append((line_no, line, body, info))

    runs = _run_values([info.value for _ln, _l, _b, info in parsed])

    auto: List[Dict[str, Any]] = []
    report: List[Dict[str, Any]] = []

    for line_no, line, body, info in parsed:
        entry = {
            "line": line_no,
            "before": line.strip(),
            "number": info.token,
        }
        if info.guarded:
            report.append({**entry, "reason": info.guard_reason,
                           "hint": "数字是内容，建议保留"})
            continue
        # 残句判定排在成序判定之前：它对两种情形都成立，且给出的处置更具体。
        # 反例见测试——`### 100 题 · 食用果蔬…` 作为孤例时，说「孤立数字、疑为内容」
        # 是误导（它就是题号），说「剥完是残句、把编号移到末尾」才可执行。
        if DANGLING_HEAD_RE.match(info.after):
            marker = DANGLING_HEAD_RE.match(info.after).group(1)
            remainder = DANGLING_HEAD_RE.sub("", info.after).strip()
            report.append({**entry, "reason": f"剥掉后只剩「{marker}：…」这种残句",
                           "hint": f"编号移到标题末尾，如 `{remainder}（第 {info.token} 题）`"})
            continue
        if not info.strong and info.value not in runs:
            report.append({**entry, "reason": "孤立数字，全篇未构成公差 1 的连续段",
                           "hint": "疑为内容（年份/数量/专名），建议保留；"
                                   "若确为章节号，请手工改标题"})
            continue
        auto.append({**entry, "after": _rewrite(line, info.after).strip()})

    for line_no, line, stripped in chapter_lines:
        entry = {"line": line_no, "before": line.strip()}
        if DANGLING_HEAD_RE.match(stripped):
            marker = DANGLING_HEAD_RE.match(stripped).group(1)
            report.append({**entry, "number": "",
                           "reason": f"章号剥掉后只剩「{marker}：…」这种残句",
                           "hint": "手工改标题"})
            continue
        auto.append({**entry, "after": _rewrite(line, stripped).strip()})

    auto.sort(key=lambda item: item["line"])
    report.sort(key=lambda item: item["line"])
    return {"auto": auto, "report": report}


def apply_heading_cleanup(text: str, plan: Optional[Dict[str, Any]] = None) -> Tuple[str, int]:
    """按文档级计划剥号：返回 `(新文本, 改动条数)`；已无序号时改动数为 0（幂等）。"""
    plan = plan or plan_heading_cleanup(text)
    targets = {item["line"]: item["after"] for item in plan["auto"]}
    if not targets:
        return text, 0
    out: List[str] = []
    changed = 0
    for line_no, line, _in_fence in iter_lines(text):
        replacement = targets.get(line_no)
        if replacement is not None:
            out.append(replacement)
            changed += 1
        else:
            out.append(line)
    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, changed
