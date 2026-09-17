"""Note Synthesizer: 导出「笔记」融合任务书（模块长文直供版）。

架构定位：工具层只负责**备料与渲染提示词**——把该篇笔记涵盖各**块**的模块长文
（`articles/模块XX_*_精读长文.md`）的路径清单、笔记边界信息与专属提示词（`MODULE_NOTE_PROMPT`，
其【排版】一节自带渲染硬约束）组装成 `notes/笔记XX_*_TASK.md`，交由宿主 Agent
（通常是每篇笔记一个子智能体）原生撰写。

粒度（v2.8）：模块层没有独立规划——**块就是模块**（`audio/_blocks/blocks.json`，每块 40–60 分钟，
一块一篇模块长文）。一篇笔记覆盖的块号来自**语义归并**（`note_plan.json`），
一篇笔记**可以跨多个块**。归并失位时按「一块一篇」兜底，流程不终止。

语料：笔记的唯一事实来源是**模块长文**（块级逐字稿才是长文的语料，笔记不再直接读逐字稿）。
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from src.generator.prompt_templates import (
    MODULE_NOTE_PROMPT,
)

# 成品体积门槛：低于该值视为空壳，需重新派发
MIN_NOTE_BYTES = 1000

# 笔记文件名前缀：`笔记XX_…`
NOTE_PREFIX = "笔记"


class BlockSynthesizer:
    @classmethod
    def _render_article_list(cls, article_paths: Iterable[Any]) -> str:
        """渲染「唯一事实来源」清单：**绝对路径** + 字节数，便于 Agent 直接读。

        为什么改给绝对路径（原先是仓库相对路径）：子智能体拿到相对路径还得自己拼基准目录，
        实测会多花一轮工具调用、拼错还要整轮重来。绝对路径可以直接读，省的是派发环节的真实时间。
        """
        lines: List[str] = []
        for raw in article_paths:
            path = Path(raw)
            try:
                size = path.stat().st_size if path.exists() else 0
            except OSError:
                size = 0
            try:
                shown = path.resolve().as_posix()
            except OSError:
                shown = path.as_posix()
            lines.append(f"- {shown}  ({size:,} 字节)")
        return "\n".join(lines) if lines else "- （本篇尚无模块长文，请先补齐 articles/ 后再派发）"

    @classmethod
    def collect_module_articles(
        cls, block_ids: Iterable[int], blocks: Sequence[Dict[str, Any]], ws: Any
    ):
        """收集这些块已落盘的模块长文，返回 (长文路径列表, 缺长文的块号列表)。

        定位走 `workspace.find_module_article`（按 `模块XX_` 前缀宽容匹配），
        与队列、对账、门禁共用同一口径。
        """
        from src.core.workspace import find_module_article

        table = {
            int(b.get("block_id") or 0): b for b in blocks if isinstance(b, dict)
        }
        found: List[Any] = []
        missing: List[int] = []
        for raw in block_ids:
            block_id = int(raw)
            block = table.get(block_id) or {"block_id": block_id}
            article = find_module_article(ws.articles_dir, block)
            if article is None:
                missing.append(block_id)
            else:
                found.append(article)
        return found, missing

    @classmethod
    def _blocks_str(cls, block_meta: Dict[str, Any]) -> str:
        """渲染「涵盖块」一行：跨块的笔记要把它写清楚，否则 Agent 会照一个块去写。"""
        from src.generator.topic_planner import SemanticTopicPlanner

        block_ids = block_meta.get("blocks") or []
        if not block_ids:
            return f"块 {int(block_meta.get('block_id', 1)):02d}"
        return SemanticTopicPlanner.describe_blocks(block_ids)

    @classmethod
    def build_synthesis_prompt(
        cls,
        block_meta: Dict[str, Any],
        article_paths: Optional[Iterable[Any]] = None,
    ) -> str:
        """组装笔记撰写提示词（模块长文直供 + 统一版式规范）。

        笔记只有这一种风格，因此不接受 `style` 参数。
        """
        eps = sorted(block_meta.get("episodes", []))
        p_str = f"P{eps[0]:02d}-P{eps[-1]:02d}" if len(eps) > 1 else (f"P{eps[0]:02d}" if eps else "P??")
        b_id_str = f"{block_meta.get('block_id', 1):02d}"

        def 安全(值: Any) -> str:
            return str(值).replace("{", "(").replace("}", ")")

        prompt = (
            MODULE_NOTE_PROMPT
            .replace("{block_id:02d}", b_id_str)
            .replace("{block_id}", b_id_str)
            .replace("{block_title}", 安全(block_meta.get("block_title", "知识笔记")))
            .replace("{blocks_str}", 安全(cls._blocks_str(block_meta)))
            .replace("{episodes_str}", p_str)
            .replace("{core_theme}", 安全(block_meta.get("core_theme", "")))
            .replace("{article_list}", cls._render_article_list(article_paths or []))
        )
        # 渲染硬约束**不再追加**：笔记提示词的【排版】一节已完整覆盖同一批要求（告警块、围栏与
        # 语言标识、公式、GFM 表格、分隔线、裸 HTML / mermaid、emoji 都写了），再追加一遍
        # RENDER_COMPAT_RULES 就是同一批要求说两遍，只会稀释重点（笔记只有这一种风格，无分支）。
        return prompt.replace("{{", "{").replace("}}", "}")

    @classmethod
    def get_task_filename(cls, block_meta: Dict[str, Any]) -> str:
        """Construct note task-file name: 笔记01_软件工程概述_TASK.md."""
        block_id = block_meta.get("block_id", 1)
        raw_title = block_meta.get("block_title", "知识笔记")
        clean_title = "".join(c for c in raw_title if c.isalnum() or c in (" ", "-", "_")).strip()
        return f"{NOTE_PREFIX}{block_id:02d}_{clean_title}_TASK.md"

    @classmethod
    def get_note_filename(cls, block_meta: Dict[str, Any]) -> str:
        """Construct the delivered note filename: 笔记01_软件工程概述_笔记.md."""
        return cls.get_task_filename(block_meta)[: -len("_TASK.md")] + "_笔记.md"

    @classmethod
    def _find_existing_note(
        cls, ws: Any, block_meta: Dict[str, Any]
    ) -> Optional[Path]:
        """定位该篇笔记已落盘的成品（规范名优先，再按编号前缀回退）。"""
        note_file = ws.notes_dir / cls.get_note_filename(block_meta)
        try:
            from src.core.task_cleanup import find_module_note
        except Exception:
            return note_file if note_file.exists() else None
        return find_module_note(
            ws,
            block_meta.get("block_id"),
            preferred_name=note_file.name,
        )

    @classmethod
    def synthesize_block(
        cls,
        block_meta: Dict[str, Any],
        articles: Optional[Iterable[Any]] = None,
        ws: Any = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """导出笔记融合任务书（模块长文直供），供宿主 Agent / 子智能体原生撰写。

        门禁：本篇涵盖各块的模块长文齐备才导出任务书；成品已存在则跳过（除非 force）。
        """
        eps = sorted(block_meta.get("episodes", []))
        p_str = f"P{eps[0]:02d}-P{eps[-1]:02d}" if len(eps) > 1 else (f"P{eps[0]:02d}" if eps else "P??")
        task_file = ws.notes_dir / cls.get_task_filename(block_meta)
        note_file = ws.notes_dir / cls.get_note_filename(block_meta)

        # 语料先落成 list：下面既要渲染清单、又要统计体积，迭代器过一次就空了
        article_list = list(articles or [])
        corpus_bytes = 0
        for item in article_list:
            try:
                path = Path(item)
                if path.exists():
                    corpus_bytes += path.stat().st_size
            except OSError:
                continue

        base_result = {
            "block_id": block_meta.get("block_id"),
            "block_title": block_meta.get("block_title"),
            "blocks": block_meta.get("blocks") or [block_meta.get("block_id")],
            "episodes": eps,
            "task_file": str(task_file),
            "note_file": str(note_file),
        }

        # 成品已存在：不再重复派发，并顺手回收残留任务书（保留编号最小的范本）
        cached_note = cls._find_existing_note(ws, block_meta)
        if (
            cached_note is not None
            and cached_note.stat().st_size >= MIN_NOTE_BYTES
            and not force
        ):
            try:
                from src.core.task_cleanup import reclaim_module_note_task
                reclaim_module_note_task(ws, int(block_meta.get("block_id", 1)))
            except Exception:
                pass
            print(f"[*] 笔记 {block_meta['block_id']:02d} ({p_str}) 成品已存在，跳过派发: {cached_note.name}")
            return {
                **base_result,
                "note_file": str(cached_note),
                "size_bytes": cached_note.stat().st_size,
                "status": "cached",
            }

        synthesis_prompt = cls.build_synthesis_prompt(
            block_meta, article_paths=article_list
        )
        header = (
            f"# 笔记 {block_meta['block_id']:02d} {block_meta.get('block_title', '')} 笔记任务书（NOTE_TASK）\n\n"
            f"> 状态：need-agent-note | 语料：本篇涵盖各块的模块长文（articles/） | 由宿主 Agent / 子智能体原生撰写\n"
            f"> 涵盖块：{cls._blocks_str(block_meta)} | 涵盖分集：{p_str}\n"
            f"> 语料体积：{corpus_bytes:,} 字节\n"
            f"> 工作区绝对路径：`{Path(ws.root_dir).as_posix()}`\n\n"
            f"## 1. 落盘要求\n\n"
            f"- 撰写提示词见下方第 2 节（含定位、收录范围、精炼硬指标、知识点完整性、版式规范）\n"
            f"- 目标文件：`{Path(note_file).as_posix()}`\n"
            f"- **逐篇完整读取**第 2 节列出的全部模块长文后再撰写：清单给的是**绝对路径**，可直接读取；\n"
            f"  不要探测目录、不要浏览工作区里的其他文件（那只会浪费时间）；成品产出后本任务书会被自动回收\n"
            f"- 执行须知：建议由**一个子智能体负责一篇笔记**；完成后只需回报"
            f"「笔记号 | 目标文件 | 字节数 | 覆盖块」，**不要回传正文**\n\n"
            f"---\n\n"
            f"## 2. 笔记撰写提示词\n\n"
        )
        content = header + synthesis_prompt

        content_unchanged = False
        if task_file.exists():
            try:
                content_unchanged = task_file.read_text(encoding="utf-8") == content
            except OSError:
                content_unchanged = False

        if content_unchanged:
            print(f"[*] 笔记 {block_meta['block_id']:02d} ({p_str}) 任务书内容无变化，跳过重写: {task_file.name}")
            return {**base_result, "size_bytes": task_file.stat().st_size, "status": "cached"}

        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(content, encoding="utf-8")
        print(f"[✓] 笔记 {block_meta['block_id']:02d} ({p_str}: {block_meta.get('block_title', '')}) 任务书已导出: {task_file.name}")

        return {**base_result, "size_bytes": task_file.stat().st_size, "status": "generated"}

    # ─────────────────────────────────────────────────────────────────────────
    # 块清单 → 笔记派发（cluster-notes 与 pipeline 阶段三共用的唯一实现）
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _prune_superseded_tasks(cls, ws: Any, notes: Sequence[Dict[str, Any]]) -> int:
        """清掉被本轮归并**取代**的笔记任务书：编号对不上，或同编号换了主题。

        为什么必须有这一步：笔记粒度是会变的（先是「一块一篇」，归并后变成「一篇装 5 个块」），
        变完之后旧粒度的任务书还留在 `notes/` 里，主 Agent 会照着它们再派一批**已经作废**的笔记。
        这里只清**任务书**，绝不碰成品；已有成品的任务书也一律不动（那是交付记录，交给 `cleanup` 回收）。

        调用方在**分批派发**（`--block-id` / `--start-block` / `--end-block`）时不得调用本函数：
        那时整批任务书里的大部分都还是有效待办，按「本轮没派到」去清会误删。
        """
        from src.generator.topic_planner import SemanticTopicPlanner

        expected = {
            cls.get_task_filename(SemanticTopicPlanner.note_task_meta(n)) for n in notes
        }
        removed = 0
        for task in sorted(ws.notes_dir.glob(f"{NOTE_PREFIX}*_TASK.md")):
            if task.name in expected:
                continue
            if re.match(rf"^{NOTE_PREFIX}(\d+)_", task.name) is None:
                continue  # 认不出编号的（历史遗留命名）不在此处清
            # 走到这里说明：文件名不是本轮该编号的规范名 → 编号被删了，或同编号换了主题，两种都作废
            if (ws.notes_dir / f"{task.name[: -len('_TASK.md')]}_笔记.md").exists():
                continue  # 成品已落盘：任务书是交付记录，交给 cleanup 回收
            try:
                task.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"[*] 已作废 {removed} 份被本轮笔记归并取代的旧任务书（旧粒度不再派发）")
        return removed

    @classmethod
    def dispatch_notes(
        cls,
        ws: Any,
        parts: List[Dict[str, Any]],
        course_title: str = "",
        force: bool = False,
        select: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        """块清单 → 语义归并 → 笔记任务书派发，返回本次执行的结构化结果。

        流程：读 `blocks.json`（模块的唯一来源）→ 把块归并成笔记 → 逐篇导出任务书。
        缺归并时按「一块一篇」兜底继续，**绝不终止**；`note_plan.json` 不会被兜底结果覆盖。
        """
        from src.generator.topic_planner import SemanticTopicPlanner

        result: Dict[str, Any] = {
            "blocks": [],
            "notes": [],
            "results": [],
            "note_status": "planned",
            "merged": False,
        }
        blocks = SemanticTopicPlanner.load_blocks(ws)
        result["blocks"] = blocks
        if not blocks:
            result["note_status"] = "no-blocks"
            print("\n[!] 本工作区没有块清单，无法归并笔记：模块边界来自音频装箱（块即模块）。")
            print(f"[*] 请先跑：python src/cli.py merge-audio \"{Path(ws.root_dir).as_posix()}\"")
            return result

        print(f"\n[✓] 模块（块）：共 {len(blocks)} 个，来自 audio/_blocks/blocks.json")
        for b in blocks:
            eps = sorted(int(e) for e in b.get("episodes") or [])
            print(f"    - 块 {int(b.get('block_id') or 0):02d} "
                  f"({SemanticTopicPlanner.describe_episodes(eps)}，"
                  f"{float(b.get('duration_min') or 0.0):.1f} 分钟): {b.get('title') or ''}")

        block_titles = SemanticTopicPlanner.collect_block_titles(ws, blocks)
        missing_articles = [
            int(b.get("block_id") or 0) for b in blocks
            if int(b.get("block_id") or 0) not in block_titles
        ]
        if missing_articles:
            print(f"    [i] 其中 {len(missing_articles)} 块尚无模块长文，归并依据暂用块标题："
                  f"{', '.join(f'{b:02d}' for b in missing_articles)}")

        notes, note_status, _note_diag = SemanticTopicPlanner.resolve_notes(
            blocks, parts, course_title=course_title, ws=ws, force=force,
            block_titles=block_titles,
        )
        result["notes"] = notes
        result["note_status"] = note_status if notes else "deferred"
        result["merged"] = any(len(n.get("blocks") or []) > 1 for n in notes)

        print(f"\n[✓] 笔记归并（{note_status}）：{len(blocks)} 个块 → {len(notes)} 篇笔记")
        for n in notes:
            eps = sorted(int(e) for e in n.get("episodes") or [])
            print(f"    - 笔记 {n['note_id']:02d} ({SemanticTopicPlanner.describe_episodes(eps)}; "
                  f"{SemanticTopicPlanner.describe_blocks(n.get('blocks') or [])}): {n['note_title']}")

        # 归并后一篇笔记会装多个块，与「一块一篇」的粒度不是一回事
        if result["merged"]:
            print(f"[i] 本次已把 {len(blocks)} 个块归并成 {len(notes)} 篇笔记（一篇可跨多个块）。")

        # 分批派发时不能整批作废旧任务书（本轮没派到的那些仍是有效待办）
        if select is None:
            cls._prune_superseded_tasks(ws, notes)

        print(f"\n[Phase] 逐篇导出笔记任务书（待处理 {len([n for n in notes if not select or select(n)])} 篇；笔记只有一种风格）...")
        for note in notes:
            if select is not None and not select(note):
                continue
            meta = SemanticTopicPlanner.note_task_meta(note)
            eps = meta["episodes"]
            p_str = f"P{eps[0]:02d}-P{eps[-1]:02d}" if len(eps) > 1 else (f"P{eps[0]:02d}" if eps else "P??")
            print(f"\n▶ 正在处理笔记 {meta['block_id']:02d} ({p_str}): 《{meta['block_title']}》...")

            articles, missing = cls.collect_module_articles(meta["blocks"], blocks, ws)
            if missing:
                missing_str = "、".join(f"块 {m:02d}" for m in missing)
                print(f"    [gate] 本篇尚有 {len(missing)} 块无模块长文（{missing_str}），跳过笔记派发")
                print(f"    [gate] 请先让 Agent 补齐 articles/ 后再重跑本命令（已产出的笔记会自动复用）")
                continue
            print(f"    [✓] 语料齐备：已锁定 {len(articles)} 篇模块长文")

            res = cls.synthesize_block(meta, articles, ws=ws, force=force)
            result["results"].append(res)

        return result
