# -*- coding: utf-8 -*-
"""交付质量门禁内核（`cli.py check` 的唯一实现处）。

原三个独立脚本（依据级校验 / 笔记成色 / 渲染合规）收敛到这里，共用一套工作区选择与报告逻辑，
只暴露两个 runner：

- `run_stage1(...)`：阶段一放行门禁——模块长文是否真的基于本块逐字稿（技术实体覆盖率）。
- `run_deliver(...)`：交付前体检——笔记成色 + 渲染合规。

口径（与文档一致，改动前先查门禁断言）：
- 默认**提示级**，只有 `strict=True` 时致命项才返回非零退出码；
- 笔记致命项五类定义在 `deliverable_lint.FATAL_NOTE_KEYS`；
- 渲染致命项为「告警块 / 围栏外裸字符画 / 围栏配对」；「缺语言标识」与「标题手写序号」
  默认只统计，分别由 `require_lang` / `require_no_numbering` 纳入门禁；
- 依据级校验是**启发式**：只测英文标识符与多位数字，无逐字稿的块不参与判定。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core import fsutil
from src.core.audio_merger import AudioMerger
from src.core.deliverable_lint import (
    FATAL_NOTE_KEYS,
    STRUCTURE_KEYS,
    fatal_note_total,
    fatal_render_total,
    lint_heading_numbers,
    lint_note,
    lint_render,
    summarize_note,
    summarize_render,
)
from src.core.task_cleanup import find_workspaces
from src.core.workspace import TaskWorkspace, find_module_article

# 非交付物：任务书是派发物，逐字稿是给写作角色看的原始语料（ASR 文本里围栏不闭合属正常）。
EXCLUDE_NAME_SUFFIXES = (
    "_TASK.md", "_KERNEL_TASK.md", "_转录任务书.md", TaskWorkspace.TRANSCRIPT_SUFFIX,
)

# 依据级校验阈值
DEFAULT_MIN_COVERAGE = 0.5
DEFAULT_MIN_FREQ = 2
# 笔记断句阈值（每份）；基准语料实测为 2（微机原理）/13（软件工程）
DEFAULT_MAX_TRUNCATED = 4

# 时间戳要先把整段抹掉再抽实体：否则「00:12:35」会被当成三个数字实体灌进统计
_TIMESTAMP_BLOCK_RE = re.compile(r"[\[【]\s*(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?\s*[\]】]")
# 英文标识符：字母开头、至少 3 个字符（排除 as / is / in 这类虚词）
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
# 多位数字：单数字噪声太大，只要 2 位以上（含小数）
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 连续汉字段（骨架法的切分单元；逐字稿无标点，不能靠标点分词）
_CN_RUN_RE = re.compile(r"[^\u4e00-\u9fff]+")

# 中文实体单独用更高的频次门槛：中文语料里口水串的出现次数远高于英文虚词，
# 与英文层共用门槛会把「这个/所以/然后」灌进分母。
DEFAULT_CN_MIN_FREQ = 3

# 繁简归一表（常用字子集，纯标准库：环境无 opencc/zhconv）。
# 为什么必须有：转录源（尤其 B 站 AI 字幕）繁简混杂，同一术语会以两种字形
# 并存，不归一就算成两个词，长文照写也命中不了。
TRAD_TO_SIMP = {
    "個": "个", "這": "这", "關": "关", "係": "系", "數": "数", "據": "据", "庫": "库",
    "學": "学", "習": "习", "們": "们", "對": "对", "說": "说", "講": "讲", "課": "课",
    "時": "时", "間": "间", "問": "问", "題": "题", "點": "点", "線": "线", "條": "条",
    "進": "进", "過": "过", "還": "还", "現": "现", "發": "发", "應": "应", "該": "该",
    "為": "为", "實": "实", "機": "机", "電": "电", "軟": "软", "硬": "硬", "體": "体",
    "結": "结", "構": "构", "設": "设", "計": "计", "劃": "划", "執": "执", "態": "态",
    "屬": "属", "優": "优", "選": "选", "擇": "择", "義": "义", "賴": "赖", "範": "范",
    "鍵": "键", "碼": "码", "鎖": "锁", "視": "视", "圖": "图", "參": "参", "傳": "传",
    "輸": "输", "協": "协", "處": "处", "並": "并", "儲": "储", "讀": "读", "寫": "写",
    "檔": "档", "資": "资", "料": "料", "記": "记", "錄": "录", "匯": "汇", "總": "总",
    "檢": "检", "驗": "验", "標": "标", "準": "准", "術": "术", "語": "语", "詞": "词",
    "則": "则", "導": "导", "證": "证", "圍": "围", "繞": "绕", "轉": "转", "換": "换",
    "價": "价", "級": "级", "聯": "联", "絡": "络", "議": "议", "編": "编", "譯": "译",
    "環": "环", "境": "境", "項": "项", "開": "开", "訓": "训", "練": "练", "述": "述",
    "決": "决", "覆": "覆", "蓋": "盖", "腦": "脑", "靠": "靠", "份": "份", "恢": "恢",
    "復": "复", "誌": "志", "審": "审", "戶": "户", "動": "动", "靜": "静", "務": "务",
    "競": "竞", "爭": "争", "兩": "两", "段": "段", "別": "别", "析": "析", "異": "异",
    "況": "况", "訪": "访", "輯": "辑", "刪": "删", "觸": "触", "認": "认", "獨": "独",
    "維": "维", "護": "护", "與": "与", "評": "评", "估": "估", "調": "调", "內": "内",
    "完": "完", "斷": "断", "確": "确", "錯": "错", "誤": "误", "略": "略", "假": "假",
    "來": "来", "值": "值", "備": "备", "加": "加", "反": "反", "員": "员", "存": "存", "密": "密", "將": "将", "對": "对", "常": "常", "序": "序", "庫": "库", "弱": "弱", "從": "从", "接": "接", "擇": "择", "據": "据", "整": "整", "數": "数", "會": "会", "束": "束", "案": "案", "條": "条", "業": "业", "標": "标", "樣": "样", "正": "正", "準": "准", "無": "无", "照": "照", "碼": "码", "立": "立", "策": "策", "算": "算", "範": "范", "約": "约", "級": "级", "絡": "络", "統": "统", "網": "网", "線": "线", "織": "织", "置": "置", "聯": "联", "致": "致", "處": "处", "計": "计", "設": "设", "讓": "让", "賴": "赖", "輯": "辑", "運": "运", "達": "达", "選": "选", "邏": "逻", "鏈": "链", "關": "关", "頁": "页",
}

# 口水词（英文）：课堂高频但与技术无关。它们留在分母里就是「污染型扣分」——
# 逼写作者把 sorry / ppt 写进正文来凑覆盖率。宁可漏检噪声，也不可奖励污染。
FILLER_TOKENS = frozenset({
    "ppt", "powerpoint", "okay", "ok", "sorry", "hello", "yeah", "yes", "hmm",
    "course", "lecture", "video", "class", "test", "hello",
})

# 中文停用字：虚词、连接词、代词、数量词、口头禅。骨架法把连续汉字段里的
# 这些字删掉，剩下的就是术语核心（「這個資料庫系統」→「数据库系统」）。
CN_STOP_CHARS = frozenset(
    "的了是在和就都也你他她它这那有吧呢啊吗哦嗯呀嘛呗咱"
    "把被对从为与及或者但又再只更最太很非没不未所由向朝"
    "来去上下里中前后时会说想要能可应该当然其实"
    "我你他她它们咱您谁什么哪怎嘛噢哈嘿嘻"
    "一二三四五六七八九十之其此该每如若则乃亦且跟"
    "同比较据照着第叫叫做明白首先另外够给专业问题"
    # 高频功能动词/副词：实测逐字稿里它们出现数百次，不切开会
    # 把两侧术语粘成一个假片段（「资料库系统讲资料库系统」不被切开，
    # 术语计数从 3 掉到 1）。
    # 刻意不收构词字：式(范式/模式)、表(关系表)、接(连接)、分(部分/分解)、
    # 候(候选码)、体(实体)、内(内模式)、定(定义/定点)…它们是术语的一部分，
    # 收进来会把真术语切碎——实测「式」进表后「范式」整词直接消失。
    "讲看到用出行好两样人相过点方等边起已让并因以果虽开始继续还才刚正准备发现觉得认表示例般通常大概必须需肯东西事情法面况部分整体各几百千多少次"
)

# 停用字连续串：在段内把「连续非停用片段」切出来（骨架法的真正切分动作）。
# 必须定义在 CN_STOP_CHARS 之后——字符集要从它构造。
_CN_STOP_RUN_RE = re.compile("[" + re.escape("".join(sorted(CN_STOP_CHARS))) + "]+")


def normalize_script(text: str) -> str:
    """繁体折简体（常用字子集）。逐字稿繁简混杂时不归一，同术语会算成两个。"""
    if not text or not any(ch in TRAD_TO_SIMP for ch in text):
        return text or ""
    return "".join(TRAD_TO_SIMP.get(ch, ch) for ch in text)


def resolve_workspaces(
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
) -> List[TaskWorkspace]:
    """统一的工作区选择：`--dir` 优先，否则按产物根扫描并用 `--task` 过滤。"""
    if dir_path:
        ws_path = Path(dir_path)
        if not ws_path.exists():
            print(f"[ERROR] 工作区不存在: {ws_path}", file=sys.stderr)
            return []
        return [TaskWorkspace.from_existing(ws_path)]
    workspaces = find_workspaces(base_dir)
    if task:
        workspaces = [w for w in workspaces if str(task) in w.root_dir.name]
    return workspaces


# ---------------------------------------------------------------------------
# 阶段一：依据级校验（模块长文 ↔ 块级逐字稿）
# ---------------------------------------------------------------------------

def extract_entities(text: str, min_freq: int) -> Counter:
    """抽取英文/数字层实体（兼容旧口径：只数讲师念出的标识符与多位数字）。

    口水词不进分母。见 `extract_cn_entities` —— 中文层是这道门禁真正的补丁。
    """
    body = _TIMESTAMP_BLOCK_RE.sub(" ", text or "")
    counter: Counter = Counter()
    for match in _TOKEN_RE.finditer(body):
        token = match.group(0).lower()
        if token in FILLER_TOKENS:
            continue
        counter[token] += 1
    for match in _NUMBER_RE.finditer(body):
        raw = match.group(0)
        if len(raw.replace(".", "")) >= 2:
            counter[raw] += 1
    return Counter({token: n for token, n in counter.items() if n >= min_freq})


def extract_cn_entities(text: str, min_freq: int = DEFAULT_CN_MIN_FREQ) -> Counter:
    """抽取中文技术实体：连续汉字段去掉停用字后剩下的「术语骨架」。

    为什么用骨架法而不是滑窗切 n-gram：逐字稿是无标点口语转录，滑窗会把
    「函数依赖」切碎成「函数 / 数依 / 依赖」重复计数——实测实体数从 85 膨胀
    到 6008，忠实长文的覆盖率被自己的碎片淹没（13%）。骨架法保留完整术语，
    实体数回到几十量级。

    为什么先繁简归一：转录源常见繁简混杂，「這個資料庫系統」与「这个数据库系统」
    会被算成两个术语，长文照写也命中不了其中一个。
    """
    body = normalize_script(_TIMESTAMP_BLOCK_RE.sub(" ", text or ""))
    counter: Counter = Counter()
    for run in _CN_RUN_RE.split(body):
        # 段内再按停用字切开：每个「连续非停用字片段」独立计数。
        # 不能对整段一次性去停用字——同段内重复出现的术语会被粘连成
        # 「范式范式」这种既非真实术语、又吞掉「范式」计数的假骨架。
        for piece in _CN_STOP_RUN_RE.split(run):
            if len(piece) >= 2:
                counter[piece] += 1
    return Counter({term: n for term, n in counter.items() if n >= min_freq})


def check_grounding_block(
    ws: TaskWorkspace, block: Dict[str, Any], min_freq: int, min_coverage: float
) -> Dict[str, Any]:
    """校验单个块：返回覆盖率与缺失实体明细（一块一验）。"""
    block_id = int(block.get("block_id") or 0)
    article = find_module_article(ws.articles_dir, block)
    entry: Dict[str, Any] = {
        "block_id": block_id,
        "span": str(block.get("span") or ""),
        "title": str(block.get("title") or ""),
        "episodes": [int(p) for p in (block.get("episodes") or [])],
        "article": str(article) if article else "",
        "article_bytes": 0,
        "transcript": "",
        "transcript_bytes": 0,
        "entities": 0,
        "covered": 0,
        "coverage": None,
        "missing": [],
        "status": "ok",
    }
    if article is None:
        entry["status"] = "no_article"
        return entry
    try:
        entry["article_bytes"] = article.stat().st_size
        article_text = article.read_text(encoding="utf-8", errors="replace").lower()
    except OSError as err:
        entry["status"] = "read_error"
        entry["error"] = str(err)
        return entry

    transcript = TaskWorkspace.block_path(ws, block)
    if not transcript.exists():
        # 没有逐字稿就没有比对基准：计入不可校验，绝不因此判失败
        entry["status"] = "unverifiable"
        return entry

    try:
        transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        entry["status"] = "read_error"
        entry["error"] = str(err)
        return entry
    entry["transcript"] = str(transcript)
    entry["transcript_bytes"] = transcript.stat().st_size

    # 语料要先归一再抽：长文侧同样归一后才好比对（同一术语可能一边繁一边简）。
    entities = extract_entities(transcript_text, min_freq)
    cn_entities = extract_cn_entities(transcript_text)
    if not entities and not cn_entities:
        entry["status"] = "no_entities"
        return entry

    article_norm = normalize_script(article_text)

    def _hit(token: str) -> bool:
        return token in article_text or token in article_norm

    def _layer(counter: Counter) -> tuple:
        """(覆盖率, 实体数, 命中数, 缺失清单)。空层返回 (None, 0, 0, [])。"""
        if not counter:
            return None, 0, 0, []
        missing = [token for token in counter if not _hit(token)]
        covered = len(counter) - len(missing)
        return covered / len(counter), len(counter), covered, missing

    coverage, total, covered, missing = _layer(entities)
    cn_coverage, cn_total, cn_covered, cn_missing = _layer(cn_entities)

    entry["entities"] = total
    entry["covered"] = covered
    entry["coverage"] = round(coverage, 4) if coverage is not None else None
    entry["cn_entities"] = cn_total
    entry["cn_covered"] = cn_covered
    entry["cn_coverage"] = round(cn_coverage, 4) if cn_coverage is not None else None
    # 缺失清单按语料里的出现频次排序：出现得越多却没写进长文，越可疑
    entry["missing"] = sorted(missing, key=lambda t: -entities[t])[:12]
    entry["cn_missing"] = sorted(cn_missing, key=lambda t: -cn_entities[t])[:12]

    # 双层判据：任一层过线即放行。忠实长文常把 sno/cno 改写成「学号/课程号」，
    # 英文层因此偏低——中文层正是救它的那一层；而脱稿文两层同时低，仍被拦下。
    layers = [c for c in (coverage, cn_coverage) if c is not None]
    passed = bool(layers) and max(layers) >= min_coverage
    entry["status"] = "ok" if passed else "low_coverage"
    return entry


def check_grounding_workspace(ws: TaskWorkspace, min_freq: int, min_coverage: float) -> Dict[str, Any]:
    """校验整个工作区的模块长文依据覆盖率（一块一验）。"""
    blocks = (AudioMerger.load_manifest(ws) or {}).get("blocks") or []
    entries = [
        check_grounding_block(ws, b, min_freq, min_coverage)
        for b in blocks if isinstance(b, dict)
    ]
    checked = [e for e in entries if e["status"] in ("ok", "low_coverage")]
    low = [e for e in checked if e["status"] == "low_coverage"]
    unverifiable = [e for e in entries if e["status"] == "unverifiable"]
    # 展示用的覆盖率取两层里更高的那层（与放行判据一致），否则会出现
    # 「平均覆盖 22% 却全部达标」这种自相矛盾的读数。
    def _best(e: Dict[str, Any]) -> float:
        layers = [c for c in (e.get("coverage"), e.get("cn_coverage")) if c is not None]
        return max(layers) if layers else 0.0

    avg = round(sum(_best(e) for e in checked) / len(checked), 4) if checked else None
    return {
        "workspace": ws.root_dir.name,
        "total": len(entries),
        "checked": len(checked),
        "ok": len(checked) - len(low),
        "low_coverage": len(low),
        "unverifiable": len(unverifiable),
        "no_article": len([e for e in entries if e["status"] == "no_article"]),
        "no_entities": len([e for e in entries if e["status"] == "no_entities"]),
        "read_error": len([e for e in entries if e["status"] == "read_error"]),
        "avg_coverage": avg,
        "min_coverage": min_coverage,
        "min_freq": min_freq,
        "cn_min_freq": DEFAULT_CN_MIN_FREQ,
        "entries": entries,
        "low_entries": low,
    }


def run_stage1(
    *,
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
    min_freq: int = DEFAULT_MIN_FREQ,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    strict: bool = False,
    as_json: bool = False,
) -> int:
    """阶段一放行门禁：模块长文是否基于本块逐字稿。"""
    workspaces = resolve_workspaces(base_dir=base_dir, task=task, dir_path=dir_path)
    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={base_dir or '产物根'}）", file=sys.stderr)
        return 1

    reports: List[Dict[str, Any]] = [
        check_grounding_workspace(ws, min_freq, min_coverage) for ws in workspaces
    ]

    if as_json:
        print(json.dumps({"reports": reports, "strict": bool(strict)}, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("[*] 长文依据级校验（模块长文 ↔ 块级逐字稿 技术实体覆盖率）")
        print(f"[*] 口径：英文/数字实体出现 ≥ {min_freq} 次、中文术语骨架 ≥ {DEFAULT_CN_MIN_FREQ} 次；"
              f"两层任一层覆盖率达 {min_coverage:.0%} 即放行；无逐字稿的块不参与判定")
        print("[i] 这是启发式：分层测「讲师念出的英文/数字」与「中文术语骨架」（繁简已归一、"
              "口水词已剔除），证明「用了语料」，不证明「用得对」")
        print("=" * 72)
        for report in reports:
            print(f"\n▶ {report['workspace']}")
            avg = f"{report['avg_coverage']:.1%}" if report["avg_coverage"] is not None else "—"
            print(f"    可校验 {report['checked']}/{report['total']} 块（另 {report['unverifiable']} 块无逐字稿、"
                  f"{report['no_article']} 块无模块长文、{report['no_entities']} 块逐字稿无重复技术实体、"
                  f"{report['read_error']} 块读取失败）")
            print(f"    达标 {report['ok']} | 低于下限 {report['low_coverage']} | 平均覆盖 {avg}")
            for entry in report["low_entries"][:12]:
                en_c = entry.get("coverage")
                cn_c = entry.get("cn_coverage")
                en_txt = "—" if en_c is None else f"{en_c:.0%}（{entry['covered']}/{entry['entities']}）"
                cn_txt = "—" if cn_c is None else f"{cn_c:.0%}（{entry['cn_covered']}/{entry['cn_entities']}）"
                print(f"    [✗] BLK{entry['block_id']:02d} {entry['span']}"
                      f" 英文层 {en_txt} / 中文层 {cn_txt}"
                      f" 长文 {entry['article_bytes']:,}B / 逐字稿 {entry['transcript_bytes']:,}B")
                _miss = list(entry.get("missing") or [])[:5] + list(entry.get("cn_missing") or [])[:5]
                if _miss:
                    print(f"         └ 逐字稿里高频但长文未出现：{'、'.join(_miss)}")
            if report["low_coverage"] > 12:
                print(f"    … 其余 {report['low_coverage'] - 12} 块见 --json 输出")
            if report["checked"] and report["low_coverage"] == 0:
                print("    ── 全部达标")
            if not report["checked"]:
                print("    ── 无可校验的块（尚无块级逐字稿 / 尚无模块长文 / 逐字稿里没有重复出现的"
                      "够频次的英文标识符、多位数字或中文术语——过短的块会落在这里，"
                      "不等于长文有问题）")
        print("\n" + "=" * 72)

    any_low = any(r["low_coverage"] > 0 for r in reports)
    any_no_article = any(r["no_article"] > 0 for r in reports)
    any_unready = any(r["total"] > 0 and r["checked"] == 0 for r in reports)
    if (any_low or any_no_article or any_unready) and strict:
        if any_no_article:
            print("[FAIL] 存在块尚未产出模块长文（阶段一长文未就绪）")
        elif any_unready:
            print("[FAIL] 未检测到任何可校验的长文或逐字稿（阶段一未就绪）")
        if any_low:
            print("[FAIL] 存在模块长文未达依据覆盖率下限（详见上方 [✗]）")
        return 1
    if not as_json:
        has_warnings = any_low or any_no_article or any_unready
        print("[OK] 依据级校验完成" + ("（提示级：加 --strict 可纳入门禁）" if has_warnings else ""))
    return 0


# ---------------------------------------------------------------------------
# 交付前体检：笔记成色
# ---------------------------------------------------------------------------

def collect_notes(ws: Any) -> List[Path]:
    """工作区内的模块笔记成品（排除任务书）。"""
    if not ws.notes_dir.exists():
        return []
    notes: List[Path] = []
    for path in sorted(ws.notes_dir.glob("*.md")):
        if path.name.endswith("_TASK.md"):
            continue
        try:
            if path.stat().st_size < fsutil.PRODUCT_MIN_BYTES:
                continue
        except OSError:
            continue
        notes.append(path)
    return notes


def check_note_workspace(
    ws: Any, max_truncated: int, require_structure: bool = False
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    totals: Dict[str, int] = {}
    fatal_total = 0

    for path in collect_notes(ws):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            files.append({"file": path.name, "error": str(err)})
            continue
        lint = lint_note(text)
        summary = summarize_note(lint)
        notes_fatal = fatal_note_total(summary)
        fatal_total += notes_fatal
        for key, value in summary.items():
            totals[key] = totals.get(key, 0) + value

        missing = [k for k in STRUCTURE_KEYS if not lint["structure"].get(k)]
        detail: Dict[str, Any] = {
            "file": TaskWorkspace.to_relative(path),
            "bytes": fsutil.file_size(path),
            "summary": summary,
            "fatal": notes_fatal,
            "structure_missing": missing,
            "samples": {
                key: lint[key][:3] for key in (*FATAL_NOTE_KEYS, "truncated") if lint.get(key)
            },
        }
        files.append(detail)

    failed = [
        f for f in files
        if f.get("fatal", 0) > 0
        or (require_structure and f.get("structure_missing"))
        or f.get("summary", {}).get("truncated", 0) > max_truncated
    ]
    return {
        "workspace": ws.root_dir.name,
        "workspace_path": str(ws.root_dir),
        "file_count": len(files),
        "fatal_total": fatal_total,
        "totals": totals,
        "files": files,
        "failed": [f["file"] for f in failed],
        "fatal_failed": [f["file"] for f in files if f.get("fatal", 0) > 0],
        "structure_gap": [f["file"] for f in files if f.get("structure_missing")],
        "truncated_threshold": max_truncated,
    }


# ---------------------------------------------------------------------------
# 交付前体检：渲染合规
# ---------------------------------------------------------------------------

def collect_markdown(ws: Any) -> List[Path]:
    """工作区内所有 Markdown 成品（递归，跳过任务书、逐字稿与隐藏目录）。"""
    files: List[Path] = []
    for path in fsutil.iter_files(ws.root_dir, "*.md", skip_hidden_dirs=True):
        try:
            rel_parents = path.relative_to(ws.root_dir).parts[:-1]
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parents):
            continue  # .archive / .backup_before_render_fix / .backup_single_notes 等
        if any(path.name.endswith(suffix) for suffix in EXCLUDE_NAME_SUFFIXES):
            continue
        try:
            if path.stat().st_size < 200:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def check_render_workspace(
    ws: Any, require_lang: bool = False, require_no_numbering: bool = False
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    totals = {
        "alert_blocks": 0,
        "stray_art": 0,
        "fences_unbalanced": 0,
        "fence_without_lang": 0,
        "numbered_headings": 0,
    }

    for path in collect_markdown(ws):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lint = lint_render(text)
        summary = summarize_render(lint)
        for key, value in summary.items():
            totals[key] += value
        # 标题手写序号：阅读器会自动编号，两套号会叠成「1. 第 1 章」这种双号。
        numbered = lint_heading_numbers(text)
        totals["numbered_headings"] += len(numbered)
        fatal_here = fatal_render_total(summary)
        if require_lang:
            fatal_here += summary["fence_without_lang"]
        if require_no_numbering:
            fatal_here += len(numbered)
        if fatal_here == 0:
            continue
        entries.append({
            "file": TaskWorkspace.to_relative(path),
            "fatal": fatal_here,
            "summary": summary,
            "numbered_headings": len(numbered),
            "samples": {
                "alert_blocks": lint["alert_blocks"][:3],
                "stray_art": lint["stray_art"][:3],
                "fence_without_lang": lint["fence_without_lang"][:3],
                "numbered_headings": numbered[:3],
            },
        })

    fatal = fatal_render_total(totals)
    if require_lang:
        fatal += totals["fence_without_lang"]
    if require_no_numbering:
        fatal += totals["numbered_headings"]
    return {
        "workspace": ws.root_dir.name,
        "workspace_path": str(ws.root_dir),
        "scanned_files": len(collect_markdown(ws)),
        "problem_files": len(entries),
        "totals": totals,
        "fatal_total": fatal,
        "require_lang": bool(require_lang),
        "require_no_numbering": bool(require_no_numbering),
        "entries": entries,
    }


def run_deliver(
    *,
    base_dir: Optional[str] = None,
    task: Optional[str] = None,
    dir_path: Optional[str] = None,
    max_truncated: int = DEFAULT_MAX_TRUNCATED,
    require_structure: bool = False,
    require_lang: bool = False,
    require_no_numbering: bool = False,
    strict: bool = False,
    as_json: bool = False,
) -> int:
    """交付前体检：笔记成色 + 渲染合规（两者共用一次工作区扫描）。"""
    workspaces = resolve_workspaces(base_dir=base_dir, task=task, dir_path=dir_path)
    if not workspaces:
        print(f"[ERROR] 未找到可用工作区（base-dir={base_dir or '产物根'}）", file=sys.stderr)
        return 1

    note_reports = [check_note_workspace(ws, max_truncated, require_structure) for ws in workspaces]
    render_reports = [
        check_render_workspace(ws, require_lang=require_lang, require_no_numbering=require_no_numbering)
        for ws in workspaces
    ]

    if as_json:
        print(json.dumps({
            "notes": note_reports,
            "render": render_reports,
            "strict": bool(strict),
            "require_structure": bool(require_structure),
            "require_lang": bool(require_lang),
            "require_no_numbering": bool(require_no_numbering),
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print(f"[*] 交付前体检（笔记成色 + 渲染合规；致命项门禁 = {'/'.join(FATAL_NOTE_KEYS)}）")
        print("=" * 72)
        for report in note_reports:
            print(f"\n▶ {report['workspace']}  （{report['file_count']} 份笔记）")
            if not report["file_count"]:
                print("    （无笔记成品）")
            else:
                for item in report["files"]:
                    if "error" in item:
                        print(f"    [!] {item['file']}: {item['error']}")
                        continue
                    s = item["summary"]
                    flag = "[✗]" if item["file"] in report["failed"] else "[✓]"
                    miss = ("/".join(k.replace("has_", "") for k in item["structure_missing"])) or "-"
                    print(
                        f"    {flag} {Path(item['file']).name[:44]:46s} 套话={s['boilerplate']:4d} "
                        f"空壳={s['hollow_headings']:2d} 分集标题={s['episode_headings']:3d} "
                        f"行内引用={s['inline_quote']:2d} 分集口吻={s['episode_voice']:2d} "
                        f"断句={s['truncated']:2d} 缺件={miss}"
                    )
                    for key in FATAL_NOTE_KEYS:
                        for sample in item["samples"].get(key, [])[:2]:
                            print(f"         └ {key} @{sample['line']}: {sample['text'][:88]}")
                t = report["totals"]
                print(f"    ── 合计：致命 {report['fatal_total']} 处 | 套话 {t.get('boilerplate', 0)} | "
                      f"空壳标题 {t.get('hollow_headings', 0)} | 分集标题 {t.get('episode_headings', 0)} | "
                      f"行内引用 {t.get('inline_quote', 0)} | 分集口吻 {t.get('episode_voice', 0)} | "
                      f"断句合计 {t.get('truncated', 0)}（阈值按**每份**笔记 {report['truncated_threshold']} 处判定）| "
                      f"结构缺件 {t.get('structure_missing', 0)}")
                if report["failed"]:
                    print(f"    ── 未通过：{len(report['failed'])} 份"
                          f"（{'；'.join(Path(f).name for f in report['failed'][:4])}）")
                elif report["structure_gap"]:
                    print(f"    ── 致命项全 0；另有 {len(report['structure_gap'])} 份缺 v2 结构构件"
                          f"（历史工作区遗留，加 --require-structure 可纳入门禁）")
                else:
                    print("    ── 全部通过")

        print("\n" + "-" * 72)
        print("[*] 渲染合规体检（告警块 / 围栏外字符画 / 围栏配对 / 围栏语言标识 / 标题手写序号）")
        scope = "语言标识=门禁项（--require-lang）" if require_lang else "语言标识=提示项（不参与 --strict）"
        scope += "；标题序号=门禁项" if require_no_numbering else "；标题序号=提示项"
        print(f"[*] 门禁口径：{scope}")
        for report in render_reports:
            print(f"\n▶ {report['workspace']}")
            print(f"    扫描成品 {report['scanned_files']} 份 | 有问题 {report['problem_files']} 份 | "
                  f"致命 {report['fatal_total']} 处")
            t = report["totals"]
            print(f"    ── 告警块 {t['alert_blocks']} | 围栏外字符画 {t['stray_art']} | "
                  f"围栏未闭合 {t['fences_unbalanced']} | 缺语言标识 {t['fence_without_lang']} | "
                  f"标题手写序号 {t.get('numbered_headings', 0)}")
            for entry in report["entries"][:12]:
                s = entry["summary"]
                short = entry["file"].split("/", 2)[-1] if "/" in entry["file"] else entry["file"]
                print(f"    [✗] {short[:66]:68s} 告警块={s['alert_blocks']:3d} "
                      f"裸图={s['stray_art']:2d} 未闭合={s['fences_unbalanced']} "
                      f"缺语言={s['fence_without_lang']} 序号={entry.get('numbered_headings', 0)}")
                for key in ("alert_blocks", "stray_art", "numbered_headings"):
                    for sample in entry["samples"].get(key, [])[:1]:
                        print(f"         └ {key} @{sample['line']}: {sample['text'][:84]}")
            if len(report["entries"]) > 12:
                print(f"    … 其余 {len(report['entries']) - 12} 份见 --json 输出")
            if report["problem_files"] == 0:
                print("    ── 全部合规")
        print("\n" + "=" * 72)

    notes_failed = any(r["failed"] for r in note_reports)
    render_fatal = any(r["fatal_total"] > 0 for r in render_reports)

    if strict and (notes_failed or render_fatal):
        if notes_failed:
            print("[FAIL] 笔记成色不达标（详见上方 ✗ 项）")
        if render_fatal:
            print("[FAIL] 存在渲染致命项（详见上方 [✗] 文件）")
        return 1
    if not as_json:
        problems = []
        if notes_failed:
            problems.append("笔记成色")
        if render_fatal:
            problems.append("渲染")
        suffix = f"（存在不达标项：{'、'.join(problems)}；未开启 --strict）" if problems else ""
        print("[OK] 交付前体检完成" + suffix)
    return 0
