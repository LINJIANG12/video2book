"""Note Synthesizer: 导出「笔记」融合任务书（文章直供版）。

架构定位：工具层只负责**备料与渲染提示词**——把该篇笔记涵盖各集单集精读长文（`articles/`）的
路径清单、笔记边界信息与专属提示词（`MODULE_NOTE_PROMPT`，其【排版】一节自带渲染硬约束）组装成
`notes/笔记XX_*_TASK.md`，交由宿主 Agent（通常是每篇笔记一个子智能体）原生撰写。

粒度（v2.1）：一篇笔记覆盖的集号来自**第二趟语义归并**（`note_plan.json`），一篇笔记**可以跨多个
知识模块**。归并失位时按「一个模块一篇」兜底，流程不终止。

语料变更（v1.7）：笔记的唯一事实来源是**单集精读长文**；
知识元（kernel）已从「前置门禁」降级为「可选索引」（`kernel_index` 显式传入才注入），
因为空壳知识元会把笔记质量一并拖垮。成品产出后任务书由 task_cleanup 自动回收。

风格变更（v1.8）：笔记**只有这一种风格**（旧版八种风格矩阵已删除），不再需要 `--style`。
"""

import json
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
        return "\n".join(lines) if lines else "- （本篇尚无单集精读长文，请先补齐 articles/ 后再派发）"

    @classmethod
    def collect_block_articles(cls, episodes: Iterable[Dict[str, Any]], ws: Any):
        """收集这些集的单集精读长文，返回 (文章路径列表, 缺文章的分集号列表)。

        复用 KernelExtractor 的宽容定位，兼容历史工作区无 `_精读文章` 后缀的命名。
        """
        from src.core.kernel_extractor import KernelExtractor

        found: List[Any] = []
        missing: List[int] = []
        for ep in episodes:
            page = ep.get("page")
            article = KernelExtractor.find_article(ws, page)
            if article is None:
                missing.append(page)
            else:
                found.append(article)
        return found, missing

    @classmethod
    def _blocks_str(cls, block_meta: Dict[str, Any]) -> str:
        """渲染「涵盖模块」一行：跨模块的笔记要把它写清楚，否则 Agent 会照一个模块去写。"""
        block_ids = block_meta.get("blocks") or []
        if not block_ids:
            return f"模块 {int(block_meta.get('block_id', 1)):02d}"
        return "、".join(f"模块 {int(b):02d}" for b in sorted(block_ids))

    @classmethod
    def build_synthesis_prompt(
        cls,
        block_meta: Dict[str, Any],
        article_paths: Optional[Iterable[Any]] = None,
        kernel_index: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """组装笔记撰写提示词（文章直供 + 渲染硬约束 + 统一版式规范）。

        笔记只有这一种风格，因此不再接受 `style` 参数。
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
        prompt = prompt.replace("{{", "{").replace("}}", "}")

        # 可选索引：仅当调用方显式传入知识元时才注入（默认不参与，避免空壳知识元拖垮笔记质量）
        if kernel_index:
            compact = json.dumps(kernel_index, ensure_ascii=False, indent=2)
            prompt += (
                "\n━━━━━━━━━━━━━━━━━━\n"
                "六、可选结构化索引（仅供定位，不得作为唯一事实来源）\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "下列知识元索引可能存在缺失或不完整之处；**它只用于帮助你定位主题**，"
                "一切事实仍以上方单集精读长文为准。索引与长文冲突时，一律以长文为准。\n\n"
                f"```json\n{compact}\n```\n"
            )

        # 渲染硬约束**不再追加**：笔记提示词的【排版】一节已完整覆盖同一批要求（告警块、围栏与
        # 语言标识、公式、GFM 表格、分隔线、裸 HTML / mermaid、emoji 都写了），再追加一遍
        # RENDER_COMPAT_RULES 就是同一批要求说两遍，只会稀释重点（笔记只有这一种风格，无分支）。
        # RENDER_COMPAT_RULES 仍由两套长文提示词注入，仍是渲染约束的唯一文案来源。
        return prompt

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
        kernel_index: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """导出笔记融合任务书（文章直供版），供宿主 Agent / 子智能体原生撰写。

        门禁：本笔记涵盖各集单集精读长文齐备才导出任务书；成品已存在则跳过（除非 force）。
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
            block_meta, article_paths=article_list, kernel_index=kernel_index
        )
        header = (
            f"# 笔记 {block_meta['block_id']:02d} {block_meta.get('block_title', '')} 笔记任务书（NOTE_TASK）\n\n"
            f"> 状态：need-agent-note | 语料：本篇涵盖各集的单集精读长文（articles/） | 由宿主 Agent / 子智能体原生撰写\n"
            f"> 涵盖模块：{cls._blocks_str(block_meta)} | 涵盖分集：{p_str}\n"
            f"> 语料体积：{corpus_bytes:,} 字节\n"
            f"> 工作区绝对路径：`{Path(ws.root_dir).as_posix()}`\n\n"
            f"## 1. 落盘要求\n\n"
            f"- 撰写提示词见下方第 2 节（含定位、收录范围、精炼硬指标、知识点完整性、版式规范与补充规范）\n"
            f"- 目标文件：`{Path(note_file).as_posix()}`\n"
            f"- **逐篇完整读取**第 2 节列出的全部单集精读长文后再撰写：清单给的是**绝对路径**，可直接读取；\n"
            f"  不要探测目录、不要浏览工作区里的其他文件（那只会浪费时间）；成品产出后本任务书会被自动回收\n"
            f"- 执行须知：建议由**一个子智能体负责一篇笔记**；完成后只需回报"
            f"「笔记号 | 目标文件 | 字节数 | 覆盖分集」，**不要回传正文**\n\n"
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
    # 两趟规划 → 笔记派发（cluster-notes 与 pipeline 阶段三共用的唯一实现）
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _purge_placeholder_tasks(cls, ws: Any) -> int:
        """清掉上一轮「占位切分」留下的任务书：真规划到位后它们就作废了。

        不清会积一堆 `笔记XX_占位块待规划…_TASK.md`——它们没有对应成品，任务书回收
        也永远收不掉，越跑越脏。
        """
        removed = 0
        for stale in sorted(ws.notes_dir.glob(f"{NOTE_PREFIX}*占位*_TASK.md")):
            try:
                stale.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"[*] 已清理 {removed} 份上一轮的占位任务书（真规划已到位）")
        return removed

    @classmethod
    def _prune_superseded_tasks(cls, ws: Any, notes: Sequence[Dict[str, Any]]) -> int:
        """清掉被本轮规划**取代**的笔记任务书：编号对不上，或同编号换了主题。

        为什么必须有这一步：笔记粒度是会变的（先是「一个模块一篇」，归并后变成「一篇装 5 个模块」），
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
                continue  # 认不出编号的（占位任务书等）交给 _purge_placeholder_tasks
            # 走到这里说明：文件名不是本轮该编号的规范名 → 编号被删了，或同编号换了主题，两种都作废
            if (ws.notes_dir / f"{task.name[: -len('_TASK.md')]}_笔记.md").exists():
                continue  # 成品已落盘：任务书是交付记录，交给 cleanup 回收
            try:
                task.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"[*] 已作废 {removed} 份被本轮笔记规划取代的旧任务书（旧粒度不再派发）")
        return removed

    @classmethod
    def dispatch_notes(
        cls,
        ws: Any,
        parts: List[Dict[str, Any]],
        course_title: str = "",
        force: bool = False,
        force_plan: bool = False,
        transcript_summaries: Optional[Dict[int, str]] = None,
        use_kernel_index: bool = False,
        select: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        """两趟语义规划 → 笔记任务书派发，返回本次执行的结构化结果。

        流程：第一趟划模块（教材用）→ 第二趟把模块归并成笔记 → 逐篇导出任务书。
        任何一趟缺规划都用兜底继续，**绝不终止**；两趟的规划文件都不会被兜底结果覆盖。
        """
        from src.generator.topic_planner import SemanticTopicPlanner

        result: Dict[str, Any] = {
            "blocks": [],
            "notes": [],
            "results": [],
            "block_status": "planned",
            "note_status": "planned",
            "merged": False,
        }
        if not parts:
            return result

        article_titles = SemanticTopicPlanner.collect_article_titles(
            ws, [p["page"] for p in parts]
        )

        blocks, block_status, _diag = SemanticTopicPlanner.resolve_blocks(
            parts, course_title=course_title, ws=ws, force=force_plan,
            transcript_summaries=transcript_summaries, article_titles=article_titles,
        )
        result["blocks"] = blocks
        result["block_status"] = block_status
        if not blocks:
            return result

        print(f"\n[✓] 第一趟·模块规划（{block_status}）：共 {len(blocks)} 个知识模块")
        for b in blocks:
            eps = sorted(int(e) for e in b.get("episodes") or [])
            print(f"    - 模块 {b['block_id']:02d} ({SemanticTopicPlanner.describe_episodes(eps)}): {b['block_title']}")

        if block_status in ("placeholder", "unmerged"):
            # 这一轮的模块号没有语义边界可言：完全没有规划时是顺序占位切分，只有第一趟
            # 规划时是「一个模块一篇」兜底。照它派发笔记，只会让子智能体按错误的边界写出
            # 几篇内容错位的笔记——那比停下来更糟。所以：导出规划任务书、把话说明白、
            # 正常返回（不报错、不退出）。
            result["note_status"] = "deferred"
            print("\n[!] 本轮不派发笔记任务书：笔记归并规划尚未产出，模块边界还不是语义边界，据它写的笔记会内容错位。")
            print(f"[*] 规划任务书：{ws.root_dir / 'topic_plan_TASK.md'}（缺第一趟时）")
            print(f"[*]             {ws.root_dir / 'note_plan_TASK.md'}（补第二趟归并）")
            print(f"[*] 让 Agent 完成规划、写出 topic_plan.json / note_plan.json 后重跑本命令，即完成派发。")
            return result

        # 笔记侧**不做体积切分**：一篇笔记与该篇的 note_plan.json 严格一一对应，模块边界
        # 只由语义规划决定。体积上限（300KB）只作用于教材分册，见 topic_planner.SIZE_CAP_BYTES
        # 处的实测依据：按体积切笔记会让覆盖不升、体积涨 39%、多出 22 处跨篇重复。
        cls._purge_placeholder_tasks(ws)

        # 占位块的模块边界不是语义边界：照它派发笔记会让子智能体写出内容错位的成品，
        # 比不派更糟。占位块既可能整轮出现（完全没有规划），也可能只占一部分（规划越界被
        # 抢救后补出来的），两种都不派；真规划写好后重跑即自动替换。
        notes, note_status, _note_diag = SemanticTopicPlanner.resolve_notes(
            blocks, parts, course_title=course_title, ws=ws, force=force_plan,
            article_titles=article_titles,
        )
        real_notes = [n for n in notes if "占位" not in str(n.get("note_title") or "")]
        placeholder_count = len(notes) - len(real_notes)
        notes = real_notes
        if placeholder_count:
            print(f"\n[!] 本轮跳过 {placeholder_count} 篇占位块笔记：占位块没有语义边界，"
                  f"据它写的笔记会内容错位；补齐 topic_plan.json 后重跑即替换。")
        merged = any(len(n.get("blocks") or []) > 1 for n in notes)
        result["notes"] = notes
        result["note_status"] = note_status if notes else "deferred"
        result["merged"] = merged

        print(f"\n[✓] 第二趟·笔记归并（{note_status}）：{len(blocks)} 个模块 → {len(notes)} 篇笔记")
        for n in notes:
            eps = sorted(int(e) for e in n.get("episodes") or [])
            blocks_str = "、".join(f"{int(b):02d}" for b in sorted(n.get("blocks") or []))
            print(f"    - 笔记 {n['note_id']:02d} ({SemanticTopicPlanner.describe_episodes(eps)}; 模块 {blocks_str}): {n['note_title']}")

        # 归并后一篇笔记会装多个模块，与「一个模块一篇」的粒度不是一回事
        if merged:
            print(f"[i] 本次已把 {len(blocks)} 个模块归并成 {len(notes)} 篇笔记（一篇可跨多个模块）。")

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

            note_parts = [p for p in parts if int(p["page"]) in set(eps)]
            articles, missing = cls.collect_block_articles(note_parts, ws)
            if missing:
                missing_str = ", ".join(f"P{m:02d}" for m in missing)
                print(f"    [gate] 本篇尚有 {len(missing)} 集无单集精读长文（{missing_str}），跳过笔记派发")
                print(f"    [gate] 请先让 Agent 补齐 articles/ 后再重跑本命令（已产出的笔记会自动复用）")
                continue
            print(f"    [✓] 语料齐备：已锁定 {len(articles)} 篇单集精读长文")

            kernel_index = None
            if use_kernel_index:
                from src.core.kernel_extractor import KernelExtractor
                kernels = KernelExtractor.extract_batch_kernels(note_parts, ws=ws)
                kernel_index = [k for k in kernels if k.get("status") == "extracted"] or None
                if kernel_index:
                    print(f"    [i] --kernel-index 已开启：注入 {len(kernel_index)} 条知识元索引（仅供参考定位）")

            res = cls.synthesize_block(
                meta, articles, ws=ws, force=force,
                kernel_index=kernel_index,
            )
            result["results"].append(res)

        return result
