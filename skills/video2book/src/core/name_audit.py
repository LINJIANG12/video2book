# -*- coding: utf-8 -*-
"""专名核对：列出长文里**未在对应逐字稿出现**的英文专名，供人工复核。

## 为什么需要它

阶段一的实体覆盖率门禁测的是「**长文有没有用这块的语料**」，不是「**用得对不对**」。
实测的反例：某长文写了 `git.nju.edu.cn` 这个域名，而讲师原话只是「我的这个 remote 仓库
是在 git 的」——覆盖率 96.6% 全绿，域名却是编的（读者会照着去访问一个不存在的地址）。
产品名、机构域名、论文名、模型版本号恰好是模型最容易"顺口补全"的地方，也是覆盖率最测不到的地方。

## 为什么只报不拦

判定需要语义：`Agentless`（长文）对应逐字稿里的音译讹字 `agent list`、`Mem0` 对应
`mom zero`——这是**正确的还原**，不是编造。机械拦截会把这批正确还原全部误杀。
所以本模块只产出**复核清单**，且给每一项附上「逐字稿里最接近的串」，让复核者一眼分辨
「编造」与「音译还原本来的样子」。

产出物是**一门课一份** `<工作区>/专名核对.md`——复核结论值得归档，不该只闪在控制台里。
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from src.core.quality_gate import extract_entities

# 专名抽取：三条形态，各自**保留原大小写**（`extract_entities` 会转小写，把大小写信号弄丢，
# 而 `Jujutsu` 与 `jujutsu` 的判别力完全靠它）。
#   1. 域名 / 路径 / 文件名：带点、带斜杠（`git.nju.edu.cn`、`src/main.py`）——
#      注意 `quality_gate._TOKEN_RE` 不含 `.` 与 `/`，会把它切成 4 个词、丢掉域名形态，
#      所以这里必须自己抽。
#   2. 含大写字母的标识符：产品名、类名、模型版本号（`Jujutsu`、`Agentless`、`GPT`）。
#   3. 全小写的常见技术词不列（`agent`、`token` 太泛，列进去会淹没重点）。
专名_RE = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_\-]*\.)+[A-Za-z]{2,}"      # 域名 / 文件名
    r"|(?:[A-Za-z0-9_\-]*[A-Z][A-Za-z0-9_\-]*[A-Za-z0-9])"  # 含大写的标识符
)
# 含点或斜杠：域名 / 路径 / 文件名（不用原始字符串：反斜杠在这里会被各层转义吃掉）
_DOMAIN_OR_PATH_RE = re.compile("[./]")
# 纯数字、纯标点不是专名
_MEANINGLESS_RE = re.compile(r"^[\d\W_]+$")
# 词太短噪声大（`OK`、`AI` 之类满课都是）
_MIN_LEN = 3

# 每块最多列出多少项：清单太长就没人看，超出的截断并注明
PER_BLOCK_LIMIT = 25
# 「最接近的串」的相似度下限：太低会把不相干的词贴上来，反而误导
CLOSE_CUTOFF = 0.55


def looks_like_proper_noun(token: str) -> bool:
    """该 token 是否值得列进复核清单（宽进：宁可多列，也不漏掉编造的域名）。

    **不在"首字母之后还有没有大写"上收窄**——实测那样会漏掉 `Jujutsu`、`Agentless`、
    `Mem0` 这三个真需要复核的形态（它们只首字母大写）。中文技术文本里出现的英文词
    本来就稀，宽进 + 靠"逐字稿有没有依据"筛，比在这里猜形态可靠。
    """
    if len(token) < _MIN_LEN or _MEANINGLESS_RE.match(token):
        return False
    if _DOMAIN_OR_PATH_RE.search(token):
        return True
    return any(ch.isupper() for ch in token)


def _nearest_for(token: str, 候选: List[str]) -> tuple:
    """找逐字稿里最接近的串。带点的 token 先**按段**匹配再整体匹配。

    为什么按段：`git.nju.edu.cn` 与逐字稿里的 `git` 整体相似度只有 0.35，整体匹配会
    找不到任何候选、`nearest` 留空——而复核者最需要看到的恰恰是"逐字稿里只有 `git`
    这一个片段"这个事实。
    """
    整体 = difflib.get_close_matches(token, 候选, n=1, cutoff=CLOSE_CUTOFF)
    if 整体:
        return 整体[0], round(difflib.SequenceMatcher(None, token, 整体[0]).ratio(), 2)
    if not _DOMAIN_OR_PATH_RE.search(token):
        return "", 0.0
    最优 = ("", 0.0)
    for 段 in re.split(r"[./]", token):
        if len(段) < 2:
            continue
        命中 = difflib.get_close_matches(段, 候选, n=1, cutoff=0.8)
        if 命中:
            比 = difflib.SequenceMatcher(None, 段, 命中[0]).ratio()
            if 比 > 最优[1]:
                最优 = (命中[0], round(比, 2))
    return 最优


def article_proper_nouns(text: str) -> Counter:
    """长文里的专名计数（保留原大小写作为展示形态）。"""
    计数: Counter = Counter()
    for match in 专名_RE.finditer(text or ""):
        token = match.group(0)
        if token.endswith("."):      # 句末的点不是域名的一部分
            token = token[:-1]
        if looks_like_proper_noun(token):
            计数[token] += 1
    # 同形不同大小写只报一次，取出现更多的那个写法
    合并: Dict[str, Dict[str, Any]] = {}
    for token, count in 计数.items():
        键 = token.lower()
        现有 = 合并.get(键)
        if 现有 is None:
            合并[键] = {"token": token, "count": count}
        else:
            现有["count"] += count
    return Counter({item["token"]: item["count"] for item in 合并.values()})


def _transcript_vocabulary(text: str) -> tuple:
    """逐字稿的判据词表：`(小写 token 集, 原文小写串)`。

    前者用于比对单词级形态（`XGrammar` ↔ `xgrammar`），后者用于比对带点带斜杠的形态
    （逐字稿里若原样写过域名，子串检查就能命中）。
    """
    原文 = (text or "").lower()
    return set(extract_entities(text or "", min_freq=1).keys()), 原文


def audit_block_names(article_text: str, transcript_text: str) -> Dict[str, Any]:
    """单块核对：返回 `{"ungrounded": [...], "total": int, "truncated": int}`。

    每项含：`token`（长文里的写法）、`count`（出现次数）、`nearest`（逐字稿里最接近的串）、
    `ratio`（相似度）。`nearest` 是复核的关键——它把「编造」与「音译还原」分开：
    编造的域名在逐字稿里只找得到零散片段（`git.nju.edu.cn` → `git`），
    而 `Agentless` 能找到 `agent`（它本就是 `agent list` 的还原）。
    """
    词表, 原文 = _transcript_vocabulary(transcript_text)
    候选 = sorted(词表)
    未命中: List[Dict[str, Any]] = []
    for token, count in article_proper_nouns(article_text).items():
        小写 = token.lower()
        if 小写 in 词表 or (len(小写) >= 5 and 小写 in 原文):
            continue
        近似, 比 = _nearest_for(小写, 候选)
        未命中.append({
            "token": token,
            "count": count,
            "nearest": 近似,
            "ratio": 比,
        })
    未命中.sort(key=lambda item: (
        0 if not item["nearest"] else 1,   # 逐字稿里连近似串都没有 → 最可疑，排最前
        -item["count"],
        item["token"],
    ))
    return {
        "ungrounded": 未命中[:PER_BLOCK_LIMIT],
        "total": len(未命中),
        "truncated": max(0, len(未命中) - PER_BLOCK_LIMIT),
    }


def collect_workspace_reports(ws: Any) -> List[Dict[str, Any]]:
    """逐个块比对「模块长文 ↔ 该块逐字稿」，产出报告条目。"""
    from src.core.block_plan import BlockPlan
    from src.core.workspace import TaskWorkspace, find_module_article

    条目: List[Dict[str, Any]] = []
    for block in BlockPlan.load_blocks(ws):
        block_id = int(block.get("block_id") or 0)
        article = find_module_article(ws.articles_dir, dict(block))
        if article is None:
            continue
        transcript = TaskWorkspace.block_path(ws, dict(block))
        if not transcript.is_file():
            continue
        try:
            长文 = Path(article).read_text(encoding="utf-8")
            稿子 = transcript.read_text(encoding="utf-8")
        except OSError:
            continue
        结果 = audit_block_names(长文, 稿子)
        if 结果["total"]:
            条目.append({
                "block_id": block_id,
                "title": str(block.get("title") or ""),
                "article": Path(article).name,
                "transcript": transcript.name,
                **结果,
            })
    return 条目


def render_report(course_title: str, entries: List[Dict[str, Any]]) -> str:
    """把报告条目渲染成一门课一份的 Markdown 复核清单。"""
    行: List[str] = []
    行.append(f"# 专名核对：{course_title}")
    行.append("")
    行.append("> 本清单列出**模块长文里出现、但对应逐字稿中没有**的英文专名（域名 / 产品名 / ")
    行.append("> 论文名 / 模型版本号）。**只报不拦**——判定需要语义，工具不替你下结论。")
    行.append(">")
    行.append("> 每项附「逐字稿里最接近的串」帮助分辨：")
    行.append("> - **编造**：最接近的串只是零散片段（如 `git.nju.edu.cn` 只找到 `git`）→ 应删或改回逐字稿的说法；")
    行.append("> - **音译还原**：最接近的串就是它的音译讹字（如 `Agentless` ← `agent list`、`Mem0` ← `mom zero`）→ 属正确还原，无需处理。")
    行.append("")

    if not entries:
        行.append("## 结论")
        行.append("")
        行.append("全部模块长文的英文专名都能在对应逐字稿中找到依据，无需复核。")
        行.append("")
        return "\n".join(行)

    总项数 = sum(item["total"] for item in entries)
    行.append(f"## 概览")
    行.append("")
    行.append(f"涉及 {len(entries)} 个块、{总项数} 项待复核。")
    行.append("")

    for item in entries:
        行.append(f"## 模块{item['block_id']:02d} {item['title']}")
        行.append("")
        行.append(f"逐字稿：`{item['transcript']}`")
        行.append("")
        行.append("| 长文里的专名 | 出现次数 | 逐字稿里最接近的串 | 相似度 |")
        行.append("| :--- | ---: | :--- | ---: |")
        for one in item["ungrounded"]:
            近似 = f"`{one['nearest']}`" if one["nearest"] else "（无）"
            比值 = f"{one['ratio']:.2f}" if one["nearest"] else "—"
            行.append(f"| `{one['token']}` | {one['count']} | {近似} | {比值} |")
        行.append("")
        if item.get("truncated"):
            行.append(f"（另有 {item['truncated']} 项未列出，项数超出单块上限）")
            行.append("")
    return "\n".join(行)


def run_audit_names(
    *,
    base_dir: Any = None,
    task: Any = None,
    dir_path: Any = None,
    as_json: bool = False,
) -> int:
    """为每个工作区产出 `<工作区>/专名核对.md`。只报告，永远返回 0。"""
    import json
    import sys

    from src.core.task_cleanup import find_workspaces
    from src.core.workspace import TaskWorkspace

    if dir_path:
        ws_path = Path(dir_path)
        if not ws_path.exists():
            print(f"[ERROR] 工作区不存在: {ws_path}", file=sys.stderr)
            return 1
        workspaces = [TaskWorkspace.from_existing(ws_path)]
    else:
        workspaces = find_workspaces(base_dir)
        if task:
            workspaces = [w for w in workspaces if str(task) in w.root_dir.name]
    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={base_dir or '产物根'}）", file=sys.stderr)
        return 1

    汇总: List[Dict[str, Any]] = []
    for ws in workspaces:
        条目 = collect_workspace_reports(ws)
        课程名 = ws.root_dir.name
        文本 = render_report(课程名, 条目)
        目标 = ws.root_dir / "专名核对.md"
        try:
            目标.write_text(文本, encoding="utf-8")
        except OSError as err:
            print(f"[!] 写入失败 {目标}: {err}", file=sys.stderr)
            continue
        汇总.append({
            "workspace": 课程名,
            "report": TaskWorkspace.to_relative(目标),
            "blocks": len(条目),
            "items": sum(item["total"] for item in 条目),
        })

    if as_json:
        print(json.dumps({"reports": 汇总}, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("[*] 专名核对（只报告：列出长文里逐字稿查不到的英文专名）")
    print("=" * 72)
    for item in 汇总:
        print(f"\n▶ {item['workspace']}")
        print(f"    涉及 {item['blocks']} 个块、{item['items']} 项待复核")
        print(f"    清单：{item['report']}")
    if not 汇总:
        print("\n[i] 没有可核对的工作区。")
    print("\n" + "-" * 72)
    print("[i] 本命令只产出复核清单，不影响任何门禁；结论由人判断。")
    print("=" * 72)
    return 0


__all__ = [
    "audit_block_names",
    "collect_workspace_reports",
    "looks_like_proper_noun",
    "render_report",
    "run_audit_names",
]
