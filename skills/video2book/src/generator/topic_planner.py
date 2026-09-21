"""笔记归并规划器（Semantic Planner）：块即模块，只做**第二趟**归并。

模块层**没有独立规划**：阶段一点五的音频装箱（`audio/_blocks/blocks.json`，每块 40–60 分钟）
就是知识模块——块标题由块内分集名语义组合而来，直接写进块音频文件名；一块一篇模块长文
（`articles/模块XX_<块标题>_精读长文.md`）。于是**教材 = 块长文按块序整编**（`cluster-articles`），
不再需要 Agent 另划一遍模块边界。

本模块只负责**笔记**这一侧：工具层做三件事——渲染提示词、校验规划、复用缓存；真正的归并由宿主
Agent 产出，落成工作区根下的 `note_plan.json`：

* `note_plan.json` —— **块归并成笔记**。读各块的标题与块长文的 H1，把块归并成若干篇笔记，
  消费方是**笔记**（`cluster-notes`）。一篇笔记**可以跨多个块**——这是刻意的：笔记照
  「一块一篇」出，几十篇摊开来又碎又散，归并粒度必须由知识体系决定，而不是机械对应单个块。

两条规矩：

* **不给知识块设集数配额。** 块的大小由音频装箱（40–60 分钟带）与语义边界共同决定，
  这里只按知识亲缘关系归并，不按块数凑数；
* **缺归并不终止流程。** 导出归并任务书后按「一块一篇」兜底继续，兜底结果只在内存里用，
  **永不回写规划文件**——Agent 写出真归并后重跑即自动替换。
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class SemanticTopicPlanner:
    STATUS_PENDING_NOTES = "need-agent-note-plan"

    # ── 第一趟「模块规划」已废除 ────────────────────────────────────────────────
    # 旧链路让 Agent 读「块内每集长文 H1」再划一遍模块（`topic_plan.json`），消费方是教材。
    # 块级装箱落地后这件事成了重复劳动：块边界本身就是语义边界（块标题 = 块内分集名的语义组合），
    # 两套边界一旦不一致还会互相打架——教材按 topic_plan 分册、笔记按 note_plan 归并，
    # 同一条语料被切成两种粒度。所以第一趟整体删除：**教材直接按块序整编块长文**，
    # 不再读、也不再产出任何模块规划文件。

    # ── 笔记归并（唯一一趟） ───────────────────────────────────────────────────
    # 笔记侧**不做体积切分**：一篇笔记与 `note_plan.json` 的一条严格一一对应，粒度只由语义归并
    # 决定，块再大也不拆。实测依据（同一模块 23 篇/310KB 对拍）：把一篇笔记按体积切成两篇，
    # 知识点覆盖没有变好（96.1% → 94.7%），却多出 39% 的体积、22 处跨篇重复（切点由字节数决定，
    # 切出的两半互不知道对方写了什么），还会让笔记编号与块映射错位。
    NOTE_PLAN_PROMPT = """你是一位把整套课程编成体系化复习笔记的资深学科主编。
下面这门课已经按音频装箱划好了**知识块**（每块 40–60 分钟；块标题由块内分集名语义组合而来，
下方同时给出该块模块长文的主题）。请按知识体系的亲缘关系，把这些块**归并成若干篇笔记**。

【课程标题】：{course_title}
【块清单（共 {total_blocks} 块）】：
{blocks_text}

【归并要求】：
1. **以块为单位归并**：一个块整体进一篇笔记，不要拆开；**一篇笔记可以装多个块**。
2. **归并判据是知识体系的亲缘关系**，不是块编号相邻：讲同一套体系、同一条技术栈、同一条学习
   路线的块归到一篇；换了独立的大主题才另起一篇。
3. **宁可少而厚，不要多而碎**——笔记太散是最大的可读性问题：几十篇两页纸的小笔记摊开来，
   读者根本串不成体系。凡属同一体系、同一条学习路线的块**必须合并**成一篇。
   **篇数由内容决定**：不给篇数配额，也不要求凑数——合并到这个知识体系讲完了，就是一篇。
4. note_title 必须是能独立成篇的专业主题名（如“进程管理与调度体系”“关系模型与 SQL 实战”），
   **不要**“第一部分”“综合模块”“其他内容”这类空标题——它同时是笔记的检索入口。
5. **blocks 必须覆盖上面全部块编号**：不得遗漏、不得重复（一个块只能进一篇笔记）。
   集号由 blocks 自动推导，无需你填写。
6. 输出严格遵循以下 JSON Array 规范，严禁输出任何 Markdown 格式外的前后寒暄废话：

```json
[
  {{
    "note_id": 1,
    "note_title": "进程管理与调度体系",
    "blocks": [3, 4, 5],
    "core_theme": "进程状态机、调度算法族、同步互斥原语与经典问题"
  }}
]
```
"""

    # ─────────────────────────────────────────────────────────────────────────
    # 块清单与渲染
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def load_blocks(cls, ws: Any) -> List[Dict[str, Any]]:
        """读块清单（`audio/_blocks/blocks.json`）——**模块的唯一来源**。

        归一化逻辑只在 `AudioMerger.load_blocks` 一处（本方法只是它在本类上的名字）。
        """
        from src.core.audio_merger import AudioMerger

        return AudioMerger.load_blocks(ws)

    @classmethod
    def expected_pages(cls, expected: Sequence[Any]) -> set:
        """把「覆盖基准」归一化成实际集号集合。

        两种进法：

        * `[9, 10, …]` → 该集号集合；
        * `[{"page": 9, …}, …]`（即 `parts.json` 内容）→ 取 `page` 字段。

        为什么要这个函数：规划校验过去把**序号**（1..N）当成**集号**比对，于是
        「用户只指定了 P09–P87」这类工作区永远过不了——`len(parts)=79` ⇒ 期望
        {1..79}，而实际是 {9..87}，缺失与越界两头都报错。集号是 `parts.json` 的
        事实，工具无权把它重构成“必须从 1 开始”。
        """
        pages: set = set()
        for item in expected or []:
            if isinstance(item, dict):
                page = item.get("page")
                if page is None:
                    continue
                pages.add(int(page))
            else:
                pages.add(int(item))
        return pages

    @classmethod
    def describe_pages(cls, pages: Sequence[int]) -> str:
        """把集号集合渲染成人读区间串（如 `P09–P87`；不连续时逐个列出）。"""
        ordered = sorted(int(p) for p in pages)
        if not ordered:
            return "(空)"
        if len(ordered) == 1:
            return f"P{ordered[0]:02d}"
        if ordered == list(range(ordered[0], ordered[-1] + 1)):
            return f"P{ordered[0]:02d}–P{ordered[-1]:02d}"
        return ", ".join(f"P{p:02d}" for p in ordered)

    @classmethod
    def describe_episodes(cls, eps: Iterable[Any]) -> str:
        """把一组集号渲染成 `P03–P07` 形式（单集为 `P03`）。"""
        ordered = sorted(int(e) for e in eps or [])
        if not ordered:
            return "P??"
        if len(ordered) == 1:
            return f"P{ordered[0]:02d}"
        return f"P{ordered[0]:02d}–P{ordered[-1]:02d}"

    @classmethod
    def describe_blocks(cls, block_ids: Iterable[Any]) -> str:
        """把一组块号渲染成 `块 01、块 02`（笔记跨越的块在任务书里要写清楚）。"""
        ordered = sorted(int(b) for b in block_ids or [])
        if not ordered:
            return "(未覆盖任何块)"
        return "、".join(f"块 {b:02d}" for b in ordered)

    # ─────────────────────────────────────────────────────────────────────────
    # 块长文标题：归并的判断依据
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def read_article_title(cls, path: Any) -> str:
        """读一篇长文的标题：H1 优先，文件名次之。

        长文提示词要求 H1 用**本篇主题**命名（不得照抄分集标题），所以 H1 才是
        「这块到底讲了什么」的第一手表述；文件名是它的降级替身。
        """
        p = Path(path)
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(60):
                    line = fh.readline()
                    if not line:
                        break
                    text = line.strip()
                    if text.startswith("# "):
                        return text[2:].strip()
        except OSError:
            pass
        # 降级：模块03_核心语法与函数_精读长文.md → 核心语法与函数
        stem = p.stem
        for prefix in ("模块",):
            if stem.startswith(prefix) and "_" in stem:
                stem = stem.split("_", 1)[1]
        if stem.startswith("P") and "_" in stem:
            stem = stem.split("_", 1)[1]
        for suffix in ("_精读长文", "_精读文章", "_精读", "_文章"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        return stem.strip()

    @classmethod
    def collect_block_titles(
        cls, ws: Any, blocks: Sequence[Dict[str, Any]]
    ) -> Dict[int, str]:
        """收集各块**模块长文的 H1**（只读 H1，不读全文）。缺长文的块不出现在表里。

        归并依据为什么用它：块标题是「块内分集名的语义组合」（规划时的先验），
        模块长文的 H1 是 Agent 读完语料后提炼的主题（后验）——后者才反映这块真正讲了
        什么，归并才不会归错。
        """
        from src.core.workspace import find_module_article

        titles: Dict[int, str] = {}
        for block in blocks:
            article = find_module_article(ws.articles_dir, block)
            if article is None:
                continue
            title = cls.read_article_title(article)
            if title:
                titles[int(block.get("block_id") or 0)] = title
        return titles

    # ─────────────────────────────────────────────────────────────────────────
    # 第二趟：笔记归并
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def blocks_by_id(cls, blocks: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """块号 → 块对象（笔记的集号由 blocks 推导，靠它查表）。"""
        table: Dict[int, Dict[str, Any]] = {}
        for block in blocks or []:
            try:
                table[int(block.get("block_id"))] = block
            except (TypeError, ValueError):
                continue
        return table

    @classmethod
    def note_episodes(
        cls, note: Dict[str, Any], blocks: Sequence[Dict[str, Any]]
    ) -> List[int]:
        """由 `blocks` 推导本篇笔记的集号（权威口径；note 里自填的 episodes 不参与判定）。"""
        table = cls.blocks_by_id(blocks)
        eps: set = set()
        for raw in note.get("blocks") or []:
            try:
                block = table.get(int(raw))
            except (TypeError, ValueError):
                continue
            if block:
                eps.update(int(e) for e in (block.get("episodes") or []))
        return sorted(eps)

    @classmethod
    def validate_note_plan(
        cls,
        note_plan: Any,
        blocks: Sequence[Dict[str, Any]],
        expected: Sequence[Any],
    ) -> Tuple[bool, str]:
        """校验笔记归并：块**恰好被认领一次**，推导出的集号恰好覆盖全部集号。

        允许一篇笔记跨多个块（这是常态）；不允许的是漏掉某个块、或让同一个块同时进两篇笔记
        （那会让同一批内容写两遍）。
        """
        if not isinstance(note_plan, list) or not note_plan:
            return False, "笔记规划为空或非列表格式"

        table = cls.blocks_by_id(blocks)
        all_blocks = set(table)
        target = cls.expected_pages(expected)
        if not target:
            return False, "覆盖基准为空（parts.json 没有可用集号）"

        claimed: List[int] = []
        note_ids: List[int] = []
        for n_idx, note in enumerate(note_plan, 1):
            if not isinstance(note, dict):
                return False, f"笔记 {n_idx} 不是对象"
            note_ids.append(int(note.get("note_id") or 0))
            raw = note.get("blocks")
            if not raw:
                return False, f"笔记 {n_idx} 未认领任何块"
            for item in raw:
                try:
                    claimed.append(int(item))
                except (TypeError, ValueError):
                    return False, f"笔记 {n_idx} 的块编号非法：{item!r}"

        dup_ids = sorted({i for i in note_ids if note_ids.count(i) > 1})
        if dup_ids:
            return False, f"笔记编号重复（任务书与成品按编号命名，会互相覆盖）: {dup_ids}"

        unknown = sorted({b for b in claimed if b not in all_blocks})
        if unknown:
            return False, f"笔记规划引用了不存在的块编号: {unknown}"

        duplicated = sorted({b for b in claimed if claimed.count(b) > 1})
        if duplicated:
            return False, f"同一块被多篇笔记重复认领: {duplicated}"

        missing = sorted(all_blocks - set(claimed))
        if missing:
            return False, f"未被任何笔记认领的块: {missing}"

        covered: set = set()
        for note in note_plan:
            covered.update(cls.note_episodes(note, blocks))
        # 覆盖基准收窄到「块实际覆盖的集号」：--skip-failed 豁免的失败集有集号无音频、
        # 不进任何块，拿 parts 全集当基准会让按块口径完全合法的归并永远过不了校验
        # （也就永远落不到 planned），与「补齐归并后重跑即自动替换」直接矛盾。
        reachable = {int(e) for b in blocks for e in (b.get("episodes") or [])} & target
        gap = reachable - covered
        if gap:
            return False, f"笔记规划未覆盖的集号: {cls.describe_pages(sorted(gap))}"

        return True, f"笔记归并完整有效（{len(note_plan)} 篇笔记 / {len(blocks)} 个块）"

    @classmethod
    def build_note_planning_prompt(
        cls,
        blocks: Sequence[Dict[str, Any]],
        course_title: str = "",
        block_titles: Optional[Dict[int, str]] = None,
    ) -> str:
        """渲染归并提示词：块清单 + 各块模块长文的主题。"""
        lines: List[str] = []
        for block in blocks:
            eps = sorted(int(e) for e in (block.get("episodes") or []))
            block_id = int(block.get("block_id") or 0)
            minutes = float(block.get("duration_min") or 0.0)
            lines.append(
                f"- 块 {block_id:02d}（{cls.describe_episodes(eps)}，{minutes:.0f} 分钟）: "
                f"{block.get('title') or block.get('span') or ''}"
            )
            theme = str((block_titles or {}).get(block_id) or "").strip()
            if theme:
                lines.append(f"    模块长文主题：《{theme}》")

        safe_title = (course_title or "计算机专业课程").replace("{", "(").replace("}", ")")
        prompt = (
            cls.NOTE_PLAN_PROMPT
            .replace("{course_title}", safe_title)
            .replace("{blocks_text}", "\n".join(lines))
            .replace("{total_blocks}", str(len(blocks)))
        )
        return prompt.replace("{{", "{").replace("}}", "}")

    @classmethod
    def export_note_plan_task(
        cls,
        blocks: Sequence[Dict[str, Any]],
        course_title: str,
        ws: Any,
        block_titles: Optional[Dict[int, str]] = None,
    ) -> Path:
        """导出归并任务书（NOTE_PLAN_TASK）。"""
        plan_file = ws.root_dir / "note_plan.json"
        task_file = ws.root_dir / "note_plan_TASK.md"
        prompt = cls.build_note_planning_prompt(
            blocks, course_title=course_title, block_titles=block_titles
        )
        content = (
            f"# 笔记归并规划任务书（NOTE_PLAN_TASK：块 → 笔记）\n\n"
            f"> 状态：{cls.STATUS_PENDING_NOTES} | 由宿主 Agent 依据块标题与模块长文主题语义归并\n"
            f"> 本趟产物供**笔记**使用（`cluster-notes` 据此派发笔记任务书）\n"
            f"> 模块边界不需要规划：块就是模块（音频按 40–60 分钟装箱，见 `audio/_blocks/blocks.json`）\n\n"
            f"## 1. 落盘要求\n\n"
            f"- 目标文件：`{plan_file.as_posix()}`\n"
            f"- 必须为合法 JSON Array，元素形如 "
            f"`{{\"note_id\": 1, \"note_title\": \"…\", \"blocks\": [1, 2, 3], \"core_theme\": \"…\"}}`\n"
            f"- `blocks` 必须**恰好覆盖上面 {len(blocks)} 个块各一次**：不得遗漏、不得重复；\n"
            f"  集号由 blocks 自动推导，**不需要你填写**\n"
            f"- 一篇笔记**可以跨多个块**（这是常态）；一个块整体进一篇笔记，不要拆开\n"
            f"- 归并宗旨：**宁可少而厚，不要多而碎**\n"
            f"- 归并不完美也**不会卡住流程**：漏掉的块会各自补成一篇兜底笔记，流程照常往下走；\n"
            f"  补齐 note_plan.json 后重跑即替换\n"
            f"- 规划完成后重跑对应 CLI 命令即可自动复用\n\n"
            f"---\n\n"
            f"## 2. 归并提示词\n\n{prompt}\n"
        )
        task_file.write_text(content, encoding="utf-8")
        return task_file

    @classmethod
    def salvage_notes(
        cls, note_plan: Any, blocks: Sequence[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """把一份**不完全合法**的归并规划抢救成全覆盖规划，返回 (notes, 诊断行)。

        规则：能对上块的认领先到先得；引用了不存在块的认领丢掉；没人认领的块
        **各自补成一篇兜底笔记**（宁可界面上碎一点，也不能让内容凭空消失）。
        """
        table = cls.blocks_by_id(blocks)
        all_blocks = set(table)
        notes_out: List[Dict[str, Any]] = []
        diag: List[str] = []
        claimed: set = set()
        unknown_ids: List[Any] = []
        dup_ids: List[int] = []

        for note in note_plan if isinstance(note_plan, list) else []:
            if not isinstance(note, dict):
                continue
            picked: List[int] = []
            for raw in note.get("blocks") or []:
                try:
                    bid = int(raw)
                except (TypeError, ValueError):
                    unknown_ids.append(raw)
                    continue
                if bid not in all_blocks:
                    unknown_ids.append(bid)
                    continue
                if bid in claimed:
                    dup_ids.append(bid)
                    continue
                claimed.add(bid)
                picked.append(bid)
            if not picked:
                continue
            notes_out.append({
                "note_id": 0,
                "note_title": str(note.get("note_title") or "").strip() or f"笔记 {len(notes_out) + 1}",
                "blocks": sorted(picked),
                "core_theme": str(note.get("core_theme") or ""),
            })

        orphan = sorted(all_blocks - claimed)
        for bid in orphan:
            block = table[bid]
            notes_out.append({
                "note_id": 0,
                "note_title": str(block.get("title") or f"笔记 {bid}"),
                "blocks": [bid],
                "core_theme": "",
            })

        for idx, note in enumerate(notes_out, 1):
            note["note_id"] = idx
            note["episodes"] = cls.note_episodes(note, blocks)

        if unknown_ids:
            diag.append(f"引用了不存在的块、已忽略：{sorted(set(map(str, unknown_ids)))}")
        if dup_ids:
            diag.append(f"被多篇笔记重复认领、先到先得：{sorted(set(dup_ids))}")
        if orphan:
            diag.append(
                f"未被认领、已各自补成兜底笔记的块："
                f"{', '.join(f'{b:02d}' for b in orphan)}"
            )
        return notes_out, diag

    @classmethod
    def notes(
        cls,
        blocks: Sequence[Dict[str, Any]],
        parts: List[Dict[str, Any]],
        course_title: str = "",
        ws: Optional[Any] = None,
        force: bool = False,
        block_titles: Optional[Dict[int, str]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """复用盘上**完全合法**的 `note_plan.json`；否则导出归并任务书并返回 None。"""
        if not blocks:
            return []
        if ws and hasattr(ws, "root_dir"):
            plan_file = ws.root_dir / "note_plan.json"
            if plan_file.exists() and not force:
                try:
                    cached = json.loads(plan_file.read_text(encoding="utf-8"))
                    is_valid, _ = cls.validate_note_plan(cached, blocks, parts)
                    if is_valid:
                        out = []
                        for note in cached:
                            out.append({
                                "note_id": int(note.get("note_id") or len(out) + 1),
                                "note_title": str(note.get("note_title") or f"笔记 {len(out) + 1}"),
                                "blocks": sorted(int(b) for b in note.get("blocks") or []),
                                "core_theme": str(note.get("core_theme") or ""),
                                "episodes": cls.note_episodes(note, blocks),
                            })
                        return out
                except Exception:
                    pass
        if ws and hasattr(ws, "root_dir"):
            cls.export_note_plan_task(
                blocks, course_title, ws, block_titles=block_titles
            )
        return None

    @classmethod
    def resolve_notes(
        cls,
        blocks: Sequence[Dict[str, Any]],
        parts: List[Dict[str, Any]],
        course_title: str = "",
        ws: Optional[Any] = None,
        force: bool = False,
        block_titles: Optional[Dict[int, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
        """归并总入口：返回 `(notes, status, 诊断行)`，**永远拿得到可用归并**。

        * `planned`   —— 盘上是完全合法的 `note_plan.json`，直接复用；
        * `salvaged`  —— 盘上有归并但不完全合法，抢救成全覆盖归并；
        * `unmerged`  —— 盘上没有归并，兜底「一块一篇」（标注未归并，粒度偏碎）。

        三条路都不写盘上的 `note_plan.json`：那是 Agent 的语义产物，工具层不得改写。
        """
        if not blocks:
            return [], "planned", []

        notes = cls.notes(
            blocks, parts, course_title=course_title, ws=ws,
            force=force, block_titles=block_titles,
        )
        if notes is not None:
            return notes, "planned", []

        raw: Any = None
        plan_file = ws.root_dir / "note_plan.json" if ws and hasattr(ws, "root_dir") else None
        if plan_file is not None and plan_file.exists():
            try:
                raw = json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception as err:
                raw = None
                print(f"[!] note_plan.json 无法解析（{err}），本轮按「一块一篇」兜底继续")

        if isinstance(raw, list) and raw:
            _ok, reason = cls.validate_note_plan(raw, blocks, parts)
            salvaged, diag = cls.salvage_notes(raw, blocks)
            if salvaged and cls.validate_note_plan(salvaged, blocks, parts)[0]:
                print(f"[!] 盘上的笔记归并不完全合法（{reason}），已就地抢救后继续：")
                for line in diag:
                    print(f"    - {line}")
                print("[*] 盘上的 note_plan.json 未被改动；补齐后重跑即可替换本次的抢救结果。")
                return salvaged, "salvaged", diag

        unmerged: List[Dict[str, Any]] = []
        for block in blocks:
            unmerged.append({
                "note_id": len(unmerged) + 1,
                "note_title": str(block.get("title") or f"笔记 {len(unmerged) + 1}"),
                "blocks": [int(block.get("block_id"))],
                "core_theme": "",
                "episodes": sorted(int(e) for e in (block.get("episodes") or [])),
            })
        print("[!] 本工作区尚无笔记归并规划：本轮按**「一块一篇」兜底**继续（粒度偏碎，未归并）。")
        print(f"[*] 归并任务书：{ws.root_dir / 'note_plan_TASK.md' if ws else '(未导出)'}")
        print("[*] 让 Agent 完成归并并写出 note_plan.json 后重跑，即自动替换为聚合后的笔记。")
        print("[i] 注意：本轮已按兜底粒度导出笔记任务书；补齐归并后重跑即自动替换，"
              "上一轮的任务书会被自动作废——先别照着它派发。")
        return unmerged, "unmerged", []

    @classmethod
    def note_task_meta(cls, note: Dict[str, Any]) -> Dict[str, Any]:
        """把一篇笔记的规划项转成 `BlockSynthesizer` 认识的元数据。

        `block_id`/`block_title` 是笔记自身的编号与题名（笔记任务书与成品都按 `笔记XX_` 命名），
        `blocks` 才是它覆盖的**音频块**编号——两者不要混为一谈。
        """
        return {
            "block_id": int(note.get("note_id") or 1),
            "block_title": str(note.get("note_title") or "笔记"),
            "episodes": sorted(int(e) for e in (note.get("episodes") or [])),
            "core_theme": str(note.get("core_theme") or ""),
            "note_id": int(note.get("note_id") or 1),
            "note_title": str(note.get("note_title") or "笔记"),
            "blocks": sorted(int(b) for b in (note.get("blocks") or [])),
        }
