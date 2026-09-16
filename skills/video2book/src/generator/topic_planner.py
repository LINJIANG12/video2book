"""两趟语义规划器（Semantic Planner）: 第一趟划模块，第二趟归并笔记。

工具层只做三件事：渲染提示词、校验规划、复用缓存。真正的语义切分由宿主 Agent 产出，落成
工作区根下的两份 JSON：

* `topic_plan.json` —— **第一趟：模块规划**。把工作区的集号切成若干知识模块。消费方是**教材**
  （`cluster-articles` 按模块出一部精读全书）；
* `note_plan.json` —— **第二趟：笔记归并**。读第一趟各模块下**各集长文的标题**，把模块再归并成
  若干篇笔记。消费方是**笔记**（`cluster-notes`）。一篇笔记**可以跨多个模块**——这是刻意的：
  笔记照「一个模块一篇」出，几十篇摊开来又碎又散，归并粒度必须由知识体系决定，
  而不是机械对应。

两条会把人卡死的旧规矩已经废除：

* **不给知识块设集数配额。** 块的大小完全由语义边界决定（2 集可以是一块，20 集也可以是一块）。
  旧版「每块 1~3 集」是错的：它会把成体系的一大章硬切成碎片，还让规划校验变得形同虚设；
* **缺规划不终止流程。** 导出规划任务书后按**占位切分**继续往下走，占位结果只在内存里用，
  **永不回写规划文件**——Agent 写出真规划后重跑即自动转为真规划。
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class SemanticTopicPlanner:
    STATUS_PENDING = "need-agent-plan"
    STATUS_PENDING_NOTES = "need-agent-note-plan"

    # 缺规划时的占位块大小：只为了让流程继续往下走，不代表任何语义判断，也不落盘。
    PLACEHOLDER_BLOCK_SIZE = 10

    # ── 模块语料体积上限（**只作用于教材分册**；笔记侧一律不做体积切分） ────────────
    # 为什么教材要按体积切模块：一本教材要把该模块的**全部单集长文**一次性交给子智能体
    # 读完。实测语料体积超过 ~300KB 后产出质量明显下滑（细节被压平、条目被切成半句）。
    # 上限 300KB 是**硬**的；256KB 只是目标区间下沿，不强行合并去凑——把两个语义模块
    # 揉成一篇的代价比体量偏差更大。
    #
    # **笔记侧不适用这条上限**（`enforce_note_size_cap` 与笔记派发路径上的 `enforce_size_cap`
    # 均已移除，笔记严格按 `note_plan.json` 一条一篇）。实测依据（同一模块 23 篇/310KB 对拍）：
    # 把一篇笔记按体积切成两篇，知识点覆盖没有变好（96.1% → 94.7%），却多出 39% 的体积、
    # 22 处跨篇重复（切点由字节数决定，切出的两半互不知道对方写了什么），还会让笔记编号与
    # 模块映射错位。所以笔记的粒度只由语义归并决定，模块再大也不拆。
    SIZE_CAP_BYTES = 300 * 1024
    SIZE_TARGET_BYTES = 256 * 1024

    # ── 第一趟：模块规划（消费方 = 教材） ──────────────────────────────────────
    PLAN_PROMPT = """你是一位国家级计算机学科教学大纲架构专家。
请仔细分析以下课程的**分集长文主题**以及【实际语料核心提要】。
根据实际学术知识体系与内容逻辑边界，识别视频之间的真实分界线，将属于同一有机知识模块的连续分集聚合为一个知识块（Knowledge Block）。

【课程标题】：{course_title}
【分集与长文主题清单】：
{episodes_text}

【规划要求】：
1. 坚决覆盖下列**全部分集：{page_range}**（共 {total_count} 集），不得遗漏任何一集，不得重复分配任何一集。
2. **就按上面列出的集号分配**：集号以清单为准，**严禁自行补编号、重排编号，或从 1 开始另行编号**。
3. **知识块的大小完全由知识体系决定，不设集数上限、也没有「一块该有几集」的配额**：
   一个有机主题讲了几集就收几集——可能是 2 集，也可能是 20 集。判据只有一个：**语义边界**，
   换了一个独立的大主题才另起一块。**宁大勿碎**：宁可几集合成一个厚实的模块，
   也不要把成体系的一章切成零碎小块。
4. block_title 必须结合【长文主题】提炼成高度概括的专业主题（如“软件工程导论与学科范式”、“软件过程模型体系与演进”、“需求分析方法与系统建模技术”）。
5. 输出严格遵循以下 JSON Array 规范，严禁输出任何 Markdown 格式外的前后寒暄废话：

```json
[
  {{
    "block_id": 1,
    "block_title": "软件工程导论与学科范式",
    "episodes": [1, 2],
    "core_theme": "学科定位、管理学属性、历史反模式、防搭便车考核机制"
  }}
]
```
"""

    # ── 第二趟：笔记归并（消费方 = 笔记） ──────────────────────────────────────
    NOTE_PLAN_PROMPT = """你是一位把整套课程编成体系化复习笔记的资深学科主编。
下面这门课已经划好了**知识模块**（每个模块给出涵盖集号，以及该模块每集长文的主题）。
请按知识体系的亲缘关系，把这些模块**归并成若干篇笔记**。

【课程标题】：{course_title}
【模块清单（共 {total_blocks} 个模块）】：
{blocks_text}

【归并要求】：
1. **以模块为单位归并**：一个模块整体进一篇笔记，不要拆开；**一篇笔记可以装多个模块**。
2. **归并判据是知识体系的亲缘关系**，不是模块编号相邻：讲同一套体系、同一条技术栈、同一条学习
   路线的模块归到一篇；换了独立的大主题才另起一篇。
3. **宁可少而厚，不要多而碎**——笔记太散是最大的可读性问题：几十篇两页纸的小笔记摊开来，
   读者根本串不成体系。凡属同一体系、同一条学习路线的模块**必须合并**成一篇。
   **篇数由内容决定**：不给篇数配额，也不要求凑数——合并到这个知识体系讲完了，就是一篇。
4. note_title 必须是能独立成篇的专业主题名（如“进程管理与调度体系”“关系模型与 SQL 实战”），
   **不要**“第一部分”“综合模块”“其他内容”这类空标题——它同时是笔记的检索入口。
5. **blocks 必须覆盖上面全部模块编号**：不得遗漏、不得重复（一个模块只能进一篇笔记）。
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
    # 集号基准与渲染
    # ─────────────────────────────────────────────────────────────────────────
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
    def find_gaps(cls, pages: Sequence[int]) -> List[int]:
        """返回区间内的空洞集号（用于提示，不作为拒绝理由）。"""
        ordered = sorted(int(p) for p in pages)
        if len(ordered) < 2:
            return []
        present = set(ordered)
        return [p for p in range(ordered[0], ordered[-1] + 1) if p not in present]

    # ─────────────────────────────────────────────────────────────────────────
    # 长文标题：两趟规划的共同依据
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def read_article_title(cls, path: Any) -> str:
        """读一篇长文的标题：H1 优先，文件名次之。

        长文提示词要求 H1 用**本讲主题**命名（不得照抄分集标题），所以 H1 才是
        「这一集到底讲了什么」的第一手表述；文件名是它的降级替身。
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
        # 降级：P09_变量与作用域_精读文章.md → 变量与作用域
        stem = p.stem
        if stem.startswith("P") and "_" in stem:
            stem = stem.split("_", 1)[1]
        for suffix in ("_精读文章", "_精读", "_文章"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        return stem.strip()

    @classmethod
    def collect_article_titles(
        cls, ws: Any, pages: Optional[Iterable[int]] = None
    ) -> Dict[int, str]:
        """收集各集**长文标题**（只读 H1，不读全文）。

        规划依据为什么不是分集标题：分集标题是 UP 主写的一句话索引（「09. 核心语法-变量」），
        长文标题是 Agent 读完语料后提炼的主题（「变量与作用域」）——后者才反映这一集真正讲了
        什么，规划才不会切错，笔记归并才不会归错。
        """
        from src.core.kernel_extractor import KernelExtractor

        titles: Dict[int, str] = {}
        if pages is None:
            target: List[int] = sorted(
                int(p["page"])
                for p in (ws.load_parts() or [])
                if isinstance(p, dict) and p.get("page") is not None
            )
        else:
            target = sorted(int(p) for p in pages)
        for page in target:
            article = KernelExtractor.find_article(ws, page)
            if article is None:
                continue
            title = cls.read_article_title(article)
            if title:
                titles[page] = title
        return titles

    # ─────────────────────────────────────────────────────────────────────────
    # 第一趟：模块规划
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def validate_plan(
        cls, plan: List[Dict[str, Any]], expected: Sequence[Any]
    ) -> Tuple[bool, str]:
        """校验规划是否**恰好覆盖给定集号集合**：唯一、无缺失、无越界。

        `expected` 见 `expected_pages()`。集号在区间内不连续时**仍然通过**，
        只在提示里列出缺口——有洞是工作区的事实，不该把阶段二整个卡死。
        """
        if not isinstance(plan, list) or not plan:
            return False, "规划为空或非列表格式"

        target = cls.expected_pages(expected)
        if not target:
            return False, "覆盖基准为空（parts.json 没有可用集号）"

        assigned_episodes = []
        for b_idx, block in enumerate(plan, 1):
            if not isinstance(block, dict):
                return False, f"模块 {b_idx} 不是对象"
            eps = block.get("episodes", [])
            if not eps:
                return False, f"模块 {b_idx} 未包含任何分集"
            assigned_episodes.extend(eps)

        all_unique = set(assigned_episodes)
        if len(assigned_episodes) != len(all_unique):
            duplicates = sorted(x for x in all_unique if assigned_episodes.count(x) > 1)
            return False, f"发现重复分配的分集: {cls.describe_pages(duplicates)}"

        missing = target - all_unique
        if missing:
            return False, f"发现缺失未分配的分集: {cls.describe_pages(sorted(missing))}"

        extra = all_unique - target
        if extra:
            return False, (
                f"发现超出有效范围的分集编号: {cls.describe_pages(sorted(extra))}"
                f"（有效集号为 {cls.describe_pages(sorted(target))}）"
            )

        gaps = cls.find_gaps(sorted(target))
        if gaps:
            return True, (
                f"规划完整有效（注意：分集区间不连续，缺 {cls.describe_pages(gaps)}；"
                f"相关模块会缺少这几集的语料）"
            )
        return True, "规划完整有效"

    @classmethod
    def build_planning_prompt(
        cls,
        parts: List[Dict[str, Any]],
        course_title: str = "",
        transcript_summaries: Optional[Dict[int, str]] = None,
        article_titles: Optional[Dict[int, str]] = None,
    ) -> str:
        """渲染第一趟规划提示词（工具层不联网、不做本地聚类）。"""
        total_count = len(parts)
        episodes_lines = []
        for p in parts:
            p_num = int(p["page"])
            raw_title = str(p.get("title", "")).strip()
            article_title = (article_titles or {}).get(p_num, "")
            if article_title and article_title != raw_title:
                line = f"- P{p_num:02d}: {article_title}"
                if raw_title:
                    line += f"      （分集原名：{raw_title}）"
            else:
                line = f"- P{p_num:02d}: {article_title or raw_title}"
            if transcript_summaries and p_num in transcript_summaries:
                summary_snippet = transcript_summaries[p_num][:220].replace("\n", " ").strip()
                line += f"\n  [语料要点]: {summary_snippet}..."
            episodes_lines.append(line)
        safe_title = (course_title or "计算机专业课程").replace("{", "(").replace("}", ")")
        prompt = (
            cls.PLAN_PROMPT
            .replace("{course_title}", safe_title)
            .replace("{episodes_text}", "\n".join(episodes_lines))
            .replace("{page_range}", cls.describe_pages([p["page"] for p in parts]))
            .replace("{total_count}", str(total_count))
        )
        # Escape double-brace example JSON back to single braces for Agent readability
        return prompt.replace("{{", "{").replace("}}", "}")

    @classmethod
    def export_plan_task(
        cls,
        parts: List[Dict[str, Any]],
        course_title: str,
        ws: Any,
        transcript_summaries: Optional[Dict[int, str]] = None,
        article_titles: Optional[Dict[int, str]] = None,
    ) -> Path:
        """导出第一趟规划任务书（TOPIC_PLAN_TASK）。"""
        plan_file = ws.root_dir / "topic_plan.json"
        task_file = ws.root_dir / "topic_plan_TASK.md"
        prompt = cls.build_planning_prompt(
            parts,
            course_title=course_title,
            transcript_summaries=transcript_summaries,
            article_titles=article_titles,
        )
        content = (
            f"# 课程知识块规划任务书（TOPIC_PLAN_TASK / 第一趟：划模块）\n\n"
            f"> 状态：{cls.STATUS_PENDING} | 由宿主 Agent 依据真实语料语义规划；CLI 不做本地关键词聚类\n"
            f"> 本趟产物供**教材**使用；**笔记**另由第二趟 `note_plan.json` 归并（见 note_plan_TASK.md）\n\n"
            f"## 1. 落盘要求\n\n"
            f"- 目标文件：`{plan_file.as_posix()}`\n"
            f"- 必须为合法 JSON Array\n"
            f"- 必须**恰好覆盖 {cls.describe_pages([p['page'] for p in parts])}**（共 {len(parts)} 集）：不得遗漏、不得重复、不得越界；\n"
            f"  **就按上面列出的集号分配，严禁自行补编号、重排编号或从 1 开始另行编号**\n"
            f"- **块的大小由语义边界决定，不设集数上限**（几集一块、二十集一块都正常，宁大勿碎）\n"
            f"- 规划不完美也**不会卡住流程**：越界集号会被裁掉、没人认领的集号会补成占位块，\n"
            f"  流程照常往下走；但那样产出的笔记是占位粒度，补齐规划后重跑即替换\n"
            f"- 规划完成后重跑对应 CLI 命令即可自动复用\n\n"
            f"---\n\n"
            f"## 2. 规划提示词\n\n{prompt}\n"
        )
        task_file.write_text(content, encoding="utf-8")
        return task_file

    @classmethod
    def salvage_blocks(
        cls, plan: List[Dict[str, Any]], parts: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """把一份**不完全合法**的规划抢救成全覆盖规划，返回 (blocks, 诊断行)。

        抢救规则（黑马工作区的真实场景：盘上规划覆盖 P01–P185，而工作区只有 P01–P87）：

        * 完全落在工作区范围内的块 → 原样保留（Agent 的语义工作不能白做）；
        * 部分越界的块 → 裁掉越界集号；裁完还成立就保留（并提示已裁剪）；
        * 重复认领的集号 → 先到先得（按块顺序），后到的丢掉；
        * 没人认领的集号 → 按集号顺序补成占位块，保证规划仍然全覆盖。

        抢救结果**只在内存里用**，不落盘——盘上那份留给 Agent 修。
        """
        target = cls.expected_pages(parts)
        notes: List[str] = []
        kept: List[Dict[str, Any]] = []
        seen: set = set()
        clipped: List[int] = []
        dropped: List[int] = []
        taken_by_two: List[int] = []

        for block in plan if isinstance(plan, list) else []:
            if not isinstance(block, dict):
                continue
            raw_eps = [int(e) for e in (block.get("episodes") or [])]
            eps: List[int] = []
            for ep in raw_eps:
                if ep not in target:
                    continue
                if ep in seen:
                    taken_by_two.append(ep)
                    continue
                seen.add(ep)
                eps.append(ep)
            if not eps:
                dropped.append(int(block.get("block_id") or len(dropped) + 1))
                continue
            if len(eps) != len(raw_eps):
                clipped.append(int(block.get("block_id") or len(kept) + 1))
            kept.append({
                "block_id": int(block.get("block_id") or len(kept) + 1),
                "block_title": str(block.get("block_title") or f"模块 {len(kept) + 1}"),
                "episodes": sorted(eps),
                "core_theme": str(block.get("core_theme") or ""),
            })

        missing = sorted(target - seen)
        if missing:
            size = max(1, int(cls.PLACEHOLDER_BLOCK_SIZE))
            for i in range(0, len(missing), size):
                chunk = missing[i: i + size]
                kept.append({
                    "block_id": 0,  # 下面统一重编号
                    "block_title": f"占位块（待规划）{cls.describe_episodes(chunk)}",
                    "episodes": chunk,
                    "core_theme": "未规划占位：本段集号尚无 Agent 语义规划，待补齐 topic_plan.json",
                })

        # 重编号：块号必须连续，教材与笔记的文件名都靠它排序
        for idx, block in enumerate(kept, 1):
            block["block_id"] = idx

        if clipped:
            notes.append(f"已裁剪越界集号的块：{', '.join(f'{b:02d}' for b in clipped)}")
        if taken_by_two:
            notes.append(
                f"被重复认领的集号（先到先得）：{cls.describe_pages(sorted(set(taken_by_two)))}"
            )
        if dropped:
            notes.append(f"裁空后丢弃的块：{', '.join(f'{b:02d}' for b in dropped)}")
        if missing:
            notes.append(f"无人认领、已补为占位块的集号：{cls.describe_pages(missing)}")
        return kept, notes

    # ─────────────────────────────────────────────────────────────────────────
    # 模块体积归一：超限模块就地切开
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _split_episodes_by_cap(
        eps: Sequence[int], sizes: Dict[int, int], cap: int
    ) -> List[List[int]]:
        """把一串集号按累计字节均衡切成若干段，**每段不超过 cap**。

        切点永远落在集与集之间——绝不切进一集内部，所以一篇长文都不会被拆散。
        段数取 `ceil(总量 / cap)`（下界），再按该目标做均衡装填：
        「装不下必须切」（硬）＋「够一段的量就切」（软，避免出现 300KB + 10KB 的畸形分段）。
        某段内含单篇就超上限的长文时切无可切，该段会原样返回、由调用方打诊断。
        """
        if not eps:
            return []
        total = sum(sizes.get(int(e), 0) for e in eps)
        if total <= cap:
            return [[int(e) for e in eps]]

        k = max(2, -(-total // cap))
        target = total / k
        groups: List[List[int]] = []
        cur: List[int] = []
        acc = 0
        for ep in eps:
            size = sizes.get(int(ep), 0)
            if cur and (acc + size > cap or acc >= target):
                groups.append(cur)
                cur, acc = [], 0
            cur.append(int(ep))
            acc += size
        if cur:
            groups.append(cur)
        return groups

    @classmethod
    def load_cached_plan(cls, ws: Any) -> List[Dict[str, Any]]:
        """**只读**盘上的 `topic_plan.json`；不存在或损坏时返回空列表。

        为什么要单独一个只读入口：教材整编（`cluster-articles`）是纯工具环节，它需要
        与其他环节拿到**同一套模块边界**，但**不该**因此导出规划任务书——那是
        `cluster-notes` 的职责（`resolve_blocks()` 才会导出）。
        """
        plan_file = Path(ws.root_dir) / "topic_plan.json"
        if not plan_file.exists():
            return []
        try:
            raw = json.loads(plan_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        return raw if isinstance(raw, list) else []

    @classmethod
    def episode_corpus_bytes(cls, ws: Any, page: int) -> int:
        """该集单集精读长文的字节数（缺长文按 0 计，与派发门禁口径一致）。"""
        from src.core.kernel_extractor import KernelExtractor

        article = KernelExtractor.find_article(ws, int(page))
        if article is None:
            return 0
        try:
            return article.stat().st_size
        except OSError:
            return 0

    @classmethod
    def enforce_size_cap(
        cls,
        blocks: Sequence[Dict[str, Any]],
        ws: Any,
        cap: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """把语料超上限的模块就地切开，返回 `(归一后的 blocks, 诊断行)`。

        规则：

        * 模块总语料不超过上限 → 原样保留（不动它的语义边界）；
        * 超过上限 → **只在该模块内部**按集顺序切成 `k = ceil(总量 / 上限)` 段，
          切点永远落在集与集之间（绝不切进一集内部），每段严格不超过上限；
        * **不跨模块合并**：合并会把两个语义模块揉成一篇，边界比体量更值钱；
        * **不为卡体积少读**：这里只改分组，一篇长文都不会从清单里消失。

        为什么放在这一层：盘上的 `topic_plan.json` 是 Agent 的语义产物，工具层不得改写
        （与 `salvage_blocks` 同一条原则）。归一只在内存里做一次，**只在教材分册这一侧
        调用**（`cli.py` 的 `cluster-articles`）；真规划补齐后重跑即自动替换。
        """
        limit = int(cap or cls.SIZE_CAP_BYTES)
        if limit <= 0 or not blocks:
            return list(blocks or []), []

        out: List[Dict[str, Any]] = []
        diag: List[str] = []

        for block in blocks:
            eps = sorted(int(e) for e in (block.get("episodes") or []))
            if not eps:
                continue
            raw_id = int(block.get("block_id") or len(out) + 1)
            sizes = {e: cls.episode_corpus_bytes(ws, e) for e in eps}
            total = sum(sizes.values())

            if total <= limit:
                out.append({**block, "block_id": raw_id, "episodes": eps})
                continue

            groups = cls._split_episodes_by_cap(eps, sizes, limit)

            base_title = str(block.get("block_title") or f"模块 {raw_id:02d}")
            for idx, group in enumerate(groups, 1):
                seg_bytes = sum(sizes[e] for e in group)
                if seg_bytes > limit:
                    diag.append(
                        f"模块 {raw_id:02d} 第 {idx} 段仍有 {seg_bytes:,} 字节（该段内含单篇就超过"
                        f"上限的长文，已切无可切）"
                    )
                out.append({
                    **block,
                    "block_id": 0,  # 下面统一重编号
                    # 标题带上集号区间：既让人一眼看清这段的范围，也保证拆分后文件名不撞车
                    # （get_task_filename 会剥掉括号，只留下字母数字，所以括号内的集号是必要的）
                    "block_title": f"{base_title}（P{group[0]:02d}-P{group[-1]:02d}）",
                    "episodes": group,
                    "core_theme": str(block.get("core_theme") or ""),
                })
            diag.append(
                f"模块 {raw_id:02d}（{total:,} 字节）超过上限 {limit:,}，"
                f"已按集边界切成 {len(groups)} 段："
                + "、".join(f"P{g[0]:02d}-P{g[-1]:02d}" for g in groups)
            )

        # 块号必须连续：教材的文件名靠它排序
        for idx, block in enumerate(out, 1):
            block["block_id"] = idx
        return out, diag

    @classmethod
    def placeholder_blocks(
        cls, parts: List[Dict[str, Any]], size: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """盘上完全没有规划时的顺序占位切分（仅供流程继续，不落盘、无语义含义）。"""
        pages = sorted(int(p["page"]) for p in parts)
        step = max(1, int(size or cls.PLACEHOLDER_BLOCK_SIZE))
        blocks: List[Dict[str, Any]] = []
        for i in range(0, len(pages), step):
            chunk = pages[i: i + step]
            blocks.append({
                "block_id": len(blocks) + 1,
                "block_title": f"占位块（待规划）{cls.describe_episodes(chunk)}",
                "episodes": chunk,
                "core_theme": "未规划占位：本段集号尚无 Agent 语义规划，待补齐 topic_plan.json",
            })
        return blocks

    @classmethod
    def plan(
        cls,
        parts: List[Dict[str, Any]],
        course_title: str = "",
        ws: Optional[Any] = None,
        force: bool = False,
        transcript_summaries: Optional[Dict[int, str]] = None,
        article_titles: Optional[Dict[int, str]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """复用盘上**完全合法**的 `topic_plan.json`；否则导出任务书并返回 None。

        单集课程无需语义切分，直接给出一份平凡的规划（那是精确结果，不是猜测）。
        """
        total_count = len(parts)
        if total_count == 0:
            return []

        if ws and hasattr(ws, "root_dir"):
            plan_file = ws.root_dir / "topic_plan.json"
            if plan_file.exists() and not force:
                try:
                    cached_plan = json.loads(plan_file.read_text(encoding="utf-8"))
                    # 覆盖基准取 parts 的实际集号（不是序号 1..N）
                    is_valid, _ = cls.validate_plan(cached_plan, parts)
                    if is_valid:
                        return cached_plan
                except Exception:
                    pass

        # A single-episode course needs no semantic split; the trivial plan is exact, not a guess.
        # 刻意**不落盘** `topic_plan.json`：规划文件是 Agent 的语义产物，工具层不得创建或改写它。
        if total_count == 1:
            return [{
                "block_id": 1,
                "block_title": parts[0]["title"],
                # 用真实集号：该集可能是 P09 而不是 P01
                "episodes": [int(parts[0]["page"])],
                "core_theme": parts[0]["title"],
            }]

        if ws and hasattr(ws, "root_dir"):
            cls.export_plan_task(
                parts, course_title, ws,
                transcript_summaries=transcript_summaries,
                article_titles=article_titles,
            )
        return None

    @classmethod
    def resolve_blocks(
        cls,
        parts: List[Dict[str, Any]],
        course_title: str = "",
        ws: Optional[Any] = None,
        force: bool = False,
        transcript_summaries: Optional[Dict[int, str]] = None,
        article_titles: Optional[Dict[int, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
        """第一趟总入口：返回 `(blocks, status, 诊断行)`，**永远拿得到可用规划**。

        三条出路，任何一条都不终止流程：

        * `planned`     —— 盘上是完全合法的 `topic_plan.json`，直接复用；
        * `salvaged`    —— 盘上有规划但不完全合法（越界／缺失／重复／非列表），抢救成全覆盖规划；
        * `placeholder` —— 盘上根本没有规划，按集号顺序占位切分。

        后两条都会确保规划任务书在位，Agent 写出真规划后重跑即自动转为 `planned`。
        """
        if not parts:
            return [], "planned", []

        plan = cls.plan(
            parts, course_title=course_title, ws=ws, force=force,
            transcript_summaries=transcript_summaries, article_titles=article_titles,
        )
        if plan is not None:
            return plan, "planned", []

        # 走到这里说明 plan() 已经导出（或保留）了任务书：抢救盘上那份，或占位。
        raw: Any = None
        plan_file = ws.root_dir / "topic_plan.json" if ws and hasattr(ws, "root_dir") else None
        if plan_file is not None and plan_file.exists():
            try:
                raw = json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception as err:
                raw = None
                print(f"[!] topic_plan.json 无法解析（{err}），本轮按占位切分继续")

        if isinstance(raw, list) and raw:
            _ok, reason = cls.validate_plan(raw, parts)
            blocks, salvage_notes = cls.salvage_blocks(raw, parts)
            if blocks and cls.validate_plan(blocks, parts)[0]:
                print(f"[!] 盘上的知识块规划不完全合法（{reason}），已就地抢救后继续：")
                for line in salvage_notes:
                    print(f"    - {line}")
                print(f"[*] 盘上的 topic_plan.json 未被改动；补齐后重跑即可替换本次的抢救结果。")
                return blocks, "salvaged", salvage_notes

        blocks = cls.placeholder_blocks(parts)
        print("[!] 本工作区尚无知识块规划：本轮按集号顺序**占位切分**继续（不代表任何语义判断）。")
        print(f"[*] 规划任务书：{ws.root_dir / 'topic_plan_TASK.md' if ws else '(未导出)'}")
        print("[*] 让 Agent 完成规划并写出 topic_plan.json 后重跑，即自动替换为真模块。")
        return blocks, "placeholder", []

    # ─────────────────────────────────────────────────────────────────────────
    # 第二趟：笔记归并
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def blocks_by_id(cls, blocks: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """模块号 → 模块对象（笔记的集号由 blocks 推导，靠它查表）。"""
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
        """校验笔记归并：模块**恰好被认领一次**，推导出的集号恰好覆盖全部集号。

        允许一模块进多集笔记、一篇笔记跨多个模块（后者是常态）；不允许的是
        漏掉某个模块、或让同一个模块同时进两篇笔记（那会让同一批内容写两遍）。
        """
        if not isinstance(note_plan, list) or not note_plan:
            return False, "笔记规划为空或非列表格式"

        table = cls.blocks_by_id(blocks)
        all_blocks = set(table)
        target = cls.expected_pages(expected)
        if not target:
            return False, "覆盖基准为空（parts.json 没有可用集号）"

        claimed: List[int] = []
        for n_idx, note in enumerate(note_plan, 1):
            if not isinstance(note, dict):
                return False, f"笔记 {n_idx} 不是对象"
            raw = note.get("blocks")
            if not raw:
                return False, f"笔记 {n_idx} 未认领任何模块"
            for item in raw:
                try:
                    claimed.append(int(item))
                except (TypeError, ValueError):
                    return False, f"笔记 {n_idx} 的模块编号非法：{item!r}"

        unknown = sorted({b for b in claimed if b not in all_blocks})
        if unknown:
            return False, f"笔记规划引用了不存在的模块编号: {unknown}"

        duplicated = sorted({b for b in claimed if claimed.count(b) > 1})
        if duplicated:
            return False, f"同一模块被多篇笔记重复认领: {duplicated}"

        missing = sorted(all_blocks - set(claimed))
        if missing:
            return False, f"未被任何笔记认领的模块: {missing}"

        covered: set = set()
        for note in note_plan:
            covered.update(cls.note_episodes(note, blocks))
        gap = target - covered
        if gap:
            return False, f"笔记规划未覆盖的集号: {cls.describe_pages(sorted(gap))}"

        return True, f"笔记归并完整有效（{len(note_plan)} 篇笔记 / {len(blocks)} 个模块）"

    @classmethod
    def build_note_planning_prompt(
        cls,
        blocks: Sequence[Dict[str, Any]],
        course_title: str = "",
        article_titles: Optional[Dict[int, str]] = None,
    ) -> str:
        """渲染第二趟归并提示词：模块清单 + 各模块每集**长文主题**。"""
        lines: List[str] = []
        for block in blocks:
            eps = sorted(int(e) for e in (block.get("episodes") or []))
            lines.append(
                f"- 模块 {int(block.get('block_id')):02d} "
                f"({cls.describe_episodes(eps)}): {block.get('block_title', '')}"
            )
            theme = str(block.get("core_theme") or "").strip()
            if theme:
                lines.append(f"    核心议题：{theme}")
            titles = []
            for ep in eps:
                title = (article_titles or {}).get(ep)
                if title:
                    titles.append(f"P{ep:02d}「{title}」")
            if titles:
                lines.append(f"    长文主题：{'；'.join(titles)}")

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
        article_titles: Optional[Dict[int, str]] = None,
    ) -> Path:
        """导出第二趟归并任务书（NOTE_PLAN_TASK）。"""
        plan_file = ws.root_dir / "note_plan.json"
        task_file = ws.root_dir / "note_plan_TASK.md"
        prompt = cls.build_note_planning_prompt(
            blocks, course_title=course_title, article_titles=article_titles
        )
        content = (
            f"# 笔记归并规划任务书（NOTE_PLAN_TASK / 第二趟：模块归并成笔记）\n\n"
            f"> 状态：{cls.STATUS_PENDING_NOTES} | 由宿主 Agent 依据模块与长文主题语义归并\n"
            f"> 本趟产物供**笔记**使用（`cluster-notes` 据此派发笔记任务书）\n\n"
            f"## 1. 落盘要求\n\n"
            f"- 目标文件：`{plan_file.as_posix()}`\n"
            f"- 必须为合法 JSON Array，元素形如 "
            f"`{{\"note_id\": 1, \"note_title\": \"…\", \"blocks\": [1, 2, 3], \"core_theme\": \"…\"}}`\n"
            f"- `blocks` 必须**恰好覆盖上面 {len(blocks)} 个模块各一次**：不得遗漏、不得重复；\n"
            f"  集号由 blocks 自动推导，**不需要你填写**\n"
            f"- 一篇笔记**可以跨多个模块**（这是常态）；一个模块整体进一篇笔记，不要拆开\n"
            f"- 归并宗旨：**宁可少而厚，不要多而碎**\n"
            f"- 归并不完美也**不会卡住流程**：漏掉的模块会各自补成一篇兜底笔记，流程照常往下走；\n"
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

        规则：能对上模块的认领先到先得；引用了不存在模块的认领丢掉；没人认领的模块
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
                "note_title": str(block.get("block_title") or f"笔记 {bid}"),
                "blocks": [bid],
                "core_theme": str(block.get("core_theme") or ""),
            })

        for idx, note in enumerate(notes_out, 1):
            note["note_id"] = idx
            note["episodes"] = cls.note_episodes(note, blocks)

        if unknown_ids:
            diag.append(f"引用了不存在的模块、已忽略：{sorted(set(map(str, unknown_ids)))}")
        if dup_ids:
            diag.append(f"被多篇笔记重复认领、先到先得：{sorted(set(dup_ids))}")
        if orphan:
            diag.append(
                f"未被认领、已各自补成兜底笔记的模块："
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
        article_titles: Optional[Dict[int, str]] = None,
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
                blocks, course_title, ws, article_titles=article_titles
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
        article_titles: Optional[Dict[int, str]] = None,
        stale_plan: bool = False,
    ) -> Tuple[List[Dict[str, Any]], str, List[str]]:
        """第二趟总入口：返回 `(notes, status, 诊断行)`，**永远拿得到可用归并**。

        * `planned`     —— 盘上是完全合法的 `note_plan.json`，直接复用；
        * `salvaged`    —— 盘上有归并但不完全合法，抢救成全覆盖归并；
        * `unmerged`    —— 盘上没有归并（或 `stale_plan` 判定盘上那份已作废），
          兜底「一个模块一篇」（标注未归并，粒度偏碎）。

        `stale_plan=True` 表示**调用方判定盘上那份已作废**（模块边界被重切过等），盘上的
        `note_plan.json` 是按旧模块号写的：既对不上新边界，抢救时还会把已经切开的模块
        重新并回一篇，标题也会与集号张冠李戴。那时一律不采用也不抢救。

        注：规格改为「笔记不做体积切分」之后，笔记派发路径上已不存在重切来源，
        `block_synthesizer` 不再传这个开关；参数保留给别的边界变更来源使用。
        """
        if not blocks:
            return [], "planned", []

        notes = cls.notes(
            blocks, parts, course_title=course_title, ws=ws,
            force=force or stale_plan, article_titles=article_titles,
        )
        if notes is not None:
            return notes, "planned", []

        raw: Any = None
        plan_file = ws.root_dir / "note_plan.json" if ws and hasattr(ws, "root_dir") else None
        if stale_plan:
            print("[!] 模块边界已被重切，盘上的 note_plan.json 属于旧边界：")
            print("[!] 本轮不采用、也不抢救它（抢救会把切开的模块并回去，并让标题与集号错配）。")
            print("[*] 已改按「一段一篇」兜底派发；补齐 note_plan.json 后重跑即替换。")
        elif plan_file is not None and plan_file.exists():
            try:
                raw = json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception as err:
                raw = None
                print(f"[!] note_plan.json 无法解析（{err}），本轮按「一个模块一篇」兜底继续")

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
                "note_title": str(block.get("block_title") or f"笔记 {len(unmerged) + 1}"),
                "blocks": [int(block.get("block_id"))],
                "core_theme": str(block.get("core_theme") or ""),
                "episodes": sorted(int(e) for e in (block.get("episodes") or [])),
            })
        print("[!] 本工作区尚无笔记归并规划：本轮按**「一个模块一篇」兜底**继续（粒度偏碎，未归并）。")
        print(f"[*] 归并任务书：{ws.root_dir / 'note_plan_TASK.md' if ws else '(未导出)'}")
        print("[*] 让 Agent 完成归并并写出 note_plan.json 后重跑，即自动替换为聚合后的笔记。")
        print("[i] 注意：本轮已按兜底粒度导出笔记任务书；补齐归并后重跑即自动替换，"
              "上一轮的任务书会被自动作废——先别照着它派发。")
        return unmerged, "unmerged", []

    @classmethod
    def note_task_meta(cls, note: Dict[str, Any]) -> Dict[str, Any]:
        """把一篇笔记的规划项转成 `BlockSynthesizer` 认识的元数据。"""
        return {
            "block_id": int(note.get("note_id") or 1),
            "block_title": str(note.get("note_title") or "笔记"),
            "episodes": sorted(int(e) for e in (note.get("episodes") or [])),
            "core_theme": str(note.get("core_theme") or ""),
            "note_id": int(note.get("note_id") or 1),
            "note_title": str(note.get("note_title") or "笔记"),
            "blocks": sorted(int(b) for b in (note.get("blocks") or [])),
        }
