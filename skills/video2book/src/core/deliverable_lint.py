"""Deliverable Lint: 交付物成色与渲染合规的机器体检核心。

背景：此前系统只在提示词里「叮嘱」写法，没有任何程序抽查成品，于是数据库那批模块笔记的
上千行套话、75 处分集平铺标题、行内残缺引用块一路绿灯交付。本模块把「人肉才能发现的毛病」
变成可复算的指标，供 CLI / 脚本 / 自检共同调用。

两类体检：
- `lint_note(text)`    笔记成色：套话填充、空壳容器标题、分集平铺标题、分集口吻、行内残缺引用块、断句、结构完备性；
- `lint_render(text)`  渲染合规：GitHub 告警块、代码围栏配对、围栏外裸字符画、围栏语言标识缺失。

判定分级（经三套真实语料标定）：
- **致命项**：在「人工精修基准（微机原理）」与「软件工程」两套合格语料上均为 0，在数据库坏样本上大量命中；
- **警告项**：断句与结构缺件属启发式指标，连基准语料也无法归零，故只做提示不做门禁。
"""

import re
from typing import Any, Dict, List

from src.core.heading_numbers import plan_heading_cleanup
from src.core.heading_numbers import iter_lines as _heading_lines

# ── 套话黑名单：无信息量的填充句（每条陈述都必须携带具体信息，否则不许写） ────────────
BOILERPLATE_PHRASES = (
    "概念属性与边界",
    "形式化表述与理论依据明确",
    "符合全国计算机专业考纲核心知识点",
    "该知识点非常重要",
    "该知识点十分的重要",
    "需要重点掌握",
    "这是核心考点",
    "本模块将介绍",
    "本节我们学习",
    "综上所述",
    "由此可见",
    "本章主要介绍了",
)

# ── 空壳容器标题：只起包装作用、不含信息的小节名 ────────────────────────────────
HOLLOW_HEADINGS = (
    "知识拓扑框架导图",
    "核心机制与模型运转",
    "横向对比与深度认知辨析",
    "核心考点思维导图与速查大纲",
    "重点难点梳理",
)

# ── 分集平铺标题：任何级别的标题都不得以分集编号 / 分集序号开口 ─────────────────────
EPISODE_HEADING_PATTERNS = (
    re.compile(r"^#{1,6}\s*P\d{1,3}\b"),
    re.compile(r"^#{1,6}\s*第\s*[0-9一二三四五六七八九十百零]{1,4}\s*[讲集课节]\b"),
    re.compile(r"^#{1,6}\s*Part\s*\d+", re.IGNORECASE),
    re.compile(r"^#{1,6}\s*\d+\.\d+\s*第\s*[0-9一二三四五六七八九十百零]{1,4}\s*[讲集]"),
)

# ── 分集口吻：正文里以「本集 / 上一讲 / 视频中提到」叙述 ───────────────────────────
# 「本讲」的裸用（不带「中/里/我们/将/主要」）也要拦——笔记按知识主题成篇后一篇可能跨多个块，
# 「本讲题目」「本讲最核心的考点」里的「本讲」已指代不明。
# 但 `本课程共十二讲` 说的是课程整体、不是某一集，必须放过；`文本讲了什么`/`课本讲` 也要放过。
# 故只在「本讲/集/节/课」后紧跟分集指代词时才判定。
_本讲_RE = re.compile(
    r"(?<![语文耳口看听说写读练复])"
    r"本[节讲集课]"
    r"(?=(题目|重点|最|的|中|里|我们|将|主要|讨论|提到|方法|讲的是|是|有|不|两|三|几))"
)

EPISODE_VOICE_PATTERNS = (
    re.compile(r"本(节|讲|集|课)(中|里|我们|将|主要)"),
    re.compile(r"上一(讲|节|集)"),
    re.compile(r"(视频|课程|讲师|老师)(中|里)(说|讲|提到|指出)"),
    re.compile(r"P\d{1,3}\s*(中|里)(提到|讲到|介绍)"),
    _本讲_RE,
)

# ── 笔记标题元数据：标题必须是纯知识主题名 ────────────────────────────────────────
# 讲师姓名无法通用识别（需按工作区传入），此处只拦可通用判定的形态。
NOTE_TITLE_PATTERNS = (
    # 课次序号：阿拉伯数字与中文数字都算（`第2节` / `第一节课` / `第五讲`）
    (re.compile(r"第\s*[0-9一二三四五六七八九十百零]{1,4}\s*[讲集课节]"), "课次序号"),
    (re.compile(r"【\s*中文配音版\s*】|中文配音版|原声版|精讲版|高清字幕版"), "配音/版本元数据"),
    (re.compile(r"赠\s*纸质|讲义赠|送讲义|购买|下单|退款|售后"), "赠品/促销元数据"),
    # 集号/块号开口：数字串必须完整（后面不再跟数字）且后面有分隔符或空格。
    # 这样 `2025年国考…` 不会因为 `\d{1,3}` 只吃掉 `202` 而误判，
    # 而 `01 什么是智能体` / `43. 感觉器` / `01｜智能体` 这类真序号会命中。
    (re.compile(r"^\s*\d{1,3}(?!\d)\s*(?:[.、|｜]\s*|\s)\S"), "标题以集号/块号开口"),
    # 双编号：文件名 `笔记01_01解剖学绪论` 里 `笔记NN_` 之后又紧跟一段块号。
    (re.compile(r"^\d{1,3}(?!\d)\s*(?:[.、|｜]\s*|\s)\S"), "文件名双编号（笔记号后又跟块号）"),
    (re.compile(r"[（(]\s*讲师\s*[）)]|讲师原话|作者认为|作者以前"), "第三人称元话语"),
)


def lint_note_title(title: str) -> List[str]:
    """检查一个笔记标题是否混入了不该有的元数据。返回问题描述列表。"""
    问题: List[str] = []
    for pattern, label in NOTE_TITLE_PATTERNS:
        if pattern.search(title):
            问题.append(label)
    return 问题


def note_semantic_name(filename: str) -> str:
    """从笔记文件名里剥掉序号前缀与后缀，得到用于查重的语义标题。"""
    stem = filename[:-3] if filename.endswith(".md") else filename
    for suffix in ("_笔记", "_精读长文"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return re.sub(r"^笔记\d{1,3}_", "", stem)


# 笔记文件名必须是 `笔记NN_<主题>_笔记.md`：`笔记NN_` 之后直接跟主题。
# 出现第二段序号（`笔记01_01解剖学绪论`）即双编号——笔记号与块号并列且数值不等，
# 读者会误认成同一套编号体系。文件名里的空格同样判不合格。
NOTE_FILENAME_RE = re.compile(r"^笔记\d{1,3}_(?P<topic>.+?)_笔记\.md$")


def lint_note_filename(filename: str) -> List[str]:
    """检查笔记文件名格式。返回问题描述列表。"""
    问题: List[str] = []
    if " " in filename:
        问题.append("文件名含空格")
    match = NOTE_FILENAME_RE.match(filename)
    if not match:
        问题.append("文件名不符合 笔记NN_<主题>_笔记.md")
        return 问题
    topic = match.group("topic")
    for pattern, label in NOTE_TITLE_PATTERNS:
        if pattern.search(topic):
            问题.append(label)
    return 问题

# 允许的「来源标注」形态（不算分集口吻）
SOURCE_LINE_RE = re.compile(r"^\s*[*\-+]?\s*>\s*来源\s*[:：]")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^\s*```(.*)$")
LIST_ITEM_RE = re.compile(r"^\s*[*\-+]\s+")

# 字符画特征：框线字符，或多处空格对齐 + 箭头连线
BOX_CHARS_RE = re.compile(r"[─│┌┐└┘├┤┬┴┼━┃╔╗╚╝╠╣╦╩╬═║]")
BOX_RUN_RE = re.compile(r"[─│┌┐└┘├┤┬┴┼━┃╔╗╚╝╠╣╦╩╬═║]{3,}")
CODE_SPAN_RE = re.compile(r"`[^`]*`")
ART_ARROW_RE = re.compile(r"(-->|◄►|→|←|==>)")
SPACED_ALIGN_RE = re.compile(r"\S\s{3,}\S")

# 字符画识别阈值：仅「框线符号密集」的行才算裸图。
# 实测校准：正文里用 `──` 作破折号（如 `信号地 GND ──── 直通连接信号地 GND`）密度约 0.09，
# 而真正的框图/树状图密度普遍 > 0.35，故取 0.30 作为分界。
STRAY_ART_DENSITY = 0.30


def looks_like_stray_art(line: str) -> bool:
    """判断一行是否为「围栏外的裸字符画」。

    先剔除行内代码片段（`` `...` ``）——正文里说明「如 `┌`, `─`, `┐` 等制表符」属正常写作，
    再用框线符号密度与连续框线串双重约束，避免把破折号散文误判成图。
    """
    bare = CODE_SPAN_RE.sub("", line)
    non_space = len(re.sub(r"\s", "", bare))
    if non_space == 0:
        return False

    box_count = len(BOX_CHARS_RE.findall(bare))
    if box_count and box_count / non_space >= STRAY_ART_DENSITY:
        if BOX_RUN_RE.search(bare) or box_count >= 6:
            return True

    arrow_count = len(ART_ARROW_RE.findall(bare))
    if arrow_count >= 2 and len(SPACED_ALIGN_RE.findall(bare)) >= 2:
        return True
    return False

# 断句启发式：句尾悬垂虚词 / 逗号结尾 / 加粗星号未配对
DANGLING_TAIL_CHARS = set("的了和与或及在是为由被对从把将其该此即而则以自可能需应须并且等各")
DANGLING_TAIL_RE = re.compile(r"[，,]$")


def _iter_lines(text: str):
    """逐行产出 (行号, 原文, 该行是否处于代码围栏内, 该行本身是否为围栏行)。"""
    in_fence = False
    for idx, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            yield idx, line, in_fence, True
            in_fence = not in_fence
            continue
        yield idx, line, in_fence, False


def _looks_truncated(body: str) -> bool:
    """断句启发式：长句以悬垂虚词/逗号收尾，或加粗标记未配对（原文被砍断的典型特征）。"""
    text = body.strip()
    if len(text) < 30:
        return False
    if text[-1] in DANGLING_TAIL_CHARS:
        return True
    if DANGLING_TAIL_RE.search(text):
        return True
    if text.count("**") % 2 == 1:
        return True
    return False


def lint_note(text: str) -> Dict[str, Any]:
    """笔记成色体检：返回各项命中明细。"""
    boilerplate: List[Dict[str, Any]] = []
    hollow: List[Dict[str, Any]] = []
    episode_headings: List[Dict[str, Any]] = []
    inline_quote: List[Dict[str, Any]] = []
    truncated: List[Dict[str, Any]] = []
    episode_voice: List[Dict[str, Any]] = []
    title_meta: List[Dict[str, Any]] = []
    h1_seen = False

    for line_no, line, in_fence, is_fence in _iter_lines(text):
        if is_fence:
            continue
        stripped = line.strip()
        if not stripped:
            continue

        if not h1_seen and line.startswith("# "):
            h1_seen = True
            for label in lint_note_title(line[2:].strip()):
                title_meta.append({"line": line_no, "text": stripped[:120], "kind": label})

        for phrase in BOILERPLATE_PHRASES:
            if phrase in line:
                boilerplate.append({"line": line_no, "text": stripped[:120], "phrase": phrase})

        heading_match = HEADING_RE.match(line)
        if heading_match:
            heading_text = heading_match.group(2).strip()
            for pattern in EPISODE_HEADING_PATTERNS:
                if pattern.match(line):
                    episode_headings.append({"line": line_no, "text": stripped[:120]})
                    break
            if any(name in heading_text for name in HOLLOW_HEADINGS):
                hollow.append({"line": line_no, "text": stripped[:120]})

        if in_fence or SOURCE_LINE_RE.match(line):
            continue

        if LIST_ITEM_RE.match(line):
            body = LIST_ITEM_RE.sub("", line, count=1)
            # 行内残缺引用块：`* 辨析本质：> **易错点**：…`（渲染时会原样露出 `>` 与 `**`）
            if re.search(r"(?<!^)>\s*(\*\*|\[!)", body):
                inline_quote.append({"line": line_no, "text": stripped[:120]})
            if _looks_truncated(body):
                truncated.append({"line": line_no, "text": stripped[:120]})
            # 笔记正文几乎全是列表项，分集口吻必须同样在列表项上判定，
            # 否则「本讲题目」「本讲最核心的考点」这类写法在真实笔记里形同虚设。
            if any(p.search(body) for p in EPISODE_VOICE_PATTERNS):
                episode_voice.append({"line": line_no, "text": stripped[:120]})
        else:
            if _looks_truncated(stripped) and not stripped.startswith("|"):
                truncated.append({"line": line_no, "text": stripped[:120]})
            if any(p.search(line) for p in EPISODE_VOICE_PATTERNS):
                episode_voice.append({"line": line_no, "text": stripped[:120]})

    return {
        "boilerplate": boilerplate,
        "hollow_headings": hollow,
        "episode_headings": episode_headings,
        "inline_quote": inline_quote,
        "truncated": truncated,
        "episode_voice": episode_voice,
        "title_meta": title_meta,
        "structure": check_note_structure(text),
    }


def check_note_structure(text: str) -> Dict[str, Any]:
    """结构完备性：笔记版式规范要求的必备构件是否齐备。

    现行笔记版式（换版后）：只有 H1 + 按知识主题分节的条目，**不写抬头元信息引用块、
    不写知识拓扑树、不写节级主旨句**（这些是因与标题/内容重复而被刻意删掉的，所以
    不再是结构项）；标题层级最多到 `####`，且**不得手写序号**——阅读器会自动编号，
    手写序号会与它叠成 `1.1.` 那种乱码。外加原有的「末尾没有多余收尾小节」。

    所有判定共用**一次跳过代码围栏**的扫描：以前 `has_h1` / `has_sections` /
    `no_redundant_tail` 直接对全文 `re.findall`，于是围栏里的 `## 速查卡` 会误报，
    而围栏里的 `##` 又能把 `has_sections` 凑够数、让 `--require-structure` 误通过
    （第二阶段 A4）。
    """
    heading_texts: List[str] = []
    h1 = 0
    h2 = 0
    deep_headings = 0        # `#####` / `######`：超出「最多到 `####`」的上限
    # 手写序号：走文档级计划（与去号、与渲染体检共用同一份判定），只有 auto 档算违规。
    numbered = len(lint_heading_numbers(text))
    for _line_no, line, in_fence in _heading_lines(text):
        if in_fence:
            continue
        if re.match(r"^#{5,6}\s+\S", line):
            deep_headings += 1
        matched = HEADING_RE.match(line)
        if not matched:
            continue
        body = matched.group(2).strip()
        if not body:
            continue
        level = len(matched.group(1))
        if level == 1:
            h1 += 1
        elif level == 2:
            h2 += 1
        heading_texts.append(body)

    return {
        "has_h1": h1 >= 1,
        "has_sections": h2 >= 2,
        "no_h5plus_headings": deep_headings == 0,
        "no_numbered_headings": numbered == 0,
        "no_redundant_tail": not any(
            ("速查卡" in h) or ("一句话总纲" in h) for h in heading_texts
        ),
    }


def lint_heading_numbers(text: str) -> List[Dict[str, Any]]:
    """标题手写序号明细（跳过代码围栏）。

    长文与教材同样适用：它们曾经带 `## 第 N 章：…` 与继承自长文的 `## 2.1 …`，
    与阅读器的自动编号叠成双号。教材整编现已幂等去号，这里给出可复算的抽查口径。

    **走文档级计划**（`plan_heading_cleanup`）：只有 `auto` 档——形态过关且成序——才算违规。
    `report` 档（孤立数字、被计量单位/计数词救下、剥完剩残句）只提示不计入，
    否则 `## 80 小时和 60 小时的两道坎` 这类内容型标题会被永久误报。
    """
    plan = plan_heading_cleanup(text)
    return [{"line": item["line"], "text": item["before"][:120]} for item in plan["auto"]]


def lint_reported_headings(text: str) -> List[Dict[str, Any]]:
    """`report` 档标题：像序号但判定为「只报不改」，附理由与处置建议。

    与 `lint_heading_numbers` 共用同一份计划，所以两者永远不会给出互相矛盾的口径。
    """
    return [
        {
            "line": item["line"],
            "text": item["before"][:120],
            "number": item.get("number") or "",
            "reason": item.get("reason", ""),
            "hint": item.get("hint", ""),
        }
        for item in plan_heading_cleanup(text)["report"]
    ]


def lint_render(text: str) -> Dict[str, Any]:
    """渲染合规体检：告警块、围栏配对、围栏外裸字符画、围栏语言标识缺失。

    围栏计数依据 `_iter_lines` 给出的「本行之前的围栏状态」：进入围栏状态之前的 ``` 是开启、
    之后的是闭合，避免用局部变量重复切换导致计数错乱。
    """
    alerts: List[Dict[str, Any]] = []
    stray_art: List[Dict[str, Any]] = []
    fence_opens = 0
    fence_closes = 0
    fence_without_lang: List[Dict[str, Any]] = []

    for line_no, line, in_fence, is_fence in _iter_lines(text):
        stripped = line.strip()
        if is_fence:
            if in_fence:
                fence_closes += 1
            else:
                fence_opens += 1
                lang = stripped[3:].strip()
                if not lang:
                    fence_without_lang.append({"line": line_no, "text": stripped})
            continue
        if in_fence:
            continue
        if re.search(r">\s*\[!(TIP|NOTE|WARNING|IMPORTANT|CAUTION)\]", line):
            alerts.append({"line": line_no, "text": stripped[:120]})
        if stripped and not stripped.startswith("|") and looks_like_stray_art(line):
            stray_art.append({"line": line_no, "text": stripped[:120]})

    return {
        "alert_blocks": alerts,
        "stray_art": stray_art,
        "fences_unbalanced": fence_opens != fence_closes,
        "fence_opens": fence_opens,
        "fence_closes": fence_closes,
        "fence_without_lang": fence_without_lang,
    }


# 致命项：两套合格语料均为 0，坏样本大量命中 → 作为门禁
FATAL_NOTE_KEYS = ("boilerplate", "hollow_headings", "episode_headings", "inline_quote", "episode_voice")
# 标题元数据同样致命：标题是笔记的检索入口，混入讲师名/课次/配音标记后无法成篇使用。
TITLE_META_KEY = "title_meta"
# 结构缺件同样只提示（换版前的笔记按旧规范生成，必然缺新构件，不回溯达标）
STRUCTURE_KEYS = (
    "has_h1",
    "has_sections",
    "no_h5plus_headings",       # 标题最多到 `####`，不得出现 `#####` / `######`
    "no_numbered_headings",     # 标题不得手写序号（阅读器会自动编号，会叠字）
    "no_redundant_tail",
)

FATAL_RENDER_KEYS = ("alert_blocks", "stray_art", "fences_unbalanced")


def summarize_note(lint: Dict[str, Any]) -> Dict[str, int]:
    """把明细折叠成计数，便于日志与验收对照。"""
    return {
        "boilerplate": len(lint["boilerplate"]),
        "hollow_headings": len(lint["hollow_headings"]),
        "episode_headings": len(lint["episode_headings"]),
        "inline_quote": len(lint["inline_quote"]),
        "truncated": len(lint["truncated"]),
        "episode_voice": len(lint["episode_voice"]),
        "title_meta": len(lint.get("title_meta", [])),
        "structure_missing": sum(1 for k in STRUCTURE_KEYS if not lint["structure"].get(k)),
    }


def summarize_render(lint: Dict[str, Any]) -> Dict[str, int]:
    return {
        "alert_blocks": len(lint["alert_blocks"]),
        "stray_art": len(lint["stray_art"]),
        "fences_unbalanced": 1 if lint["fences_unbalanced"] else 0,
        "fence_without_lang": len(lint["fence_without_lang"]),
    }


def fatal_note_total(summary: Dict[str, int]) -> int:
    return sum(summary.get(k, 0) for k in FATAL_NOTE_KEYS) + summary.get(TITLE_META_KEY, 0)


def fatal_render_total(summary: Dict[str, int]) -> int:
    return sum(summary.get(k, 0) for k in FATAL_RENDER_KEYS)
