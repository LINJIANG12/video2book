#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module Textbook Integrator：按**内容结构**把模块长文整编成册（`textbooks/`）。

模块边界**不需要规划**：v4 根目录 `block_plan.json` 里的块就是知识模块，
一块一篇模块长文。教材是「块长文按块序整编」出的**书**：**册 = 书，章 = 块**。

## 分册依据：内容优先，体量兜底

1. **Agent 规划（首选）**：`textbook_plan.json` —— Agent 读各块标题与长文主题，按知识体系把块
   分成若干册并给**能看出内容的册名**（任务书 `textbook_plan_TASK.md` 由本模块导出）。
   规划不合法时当场抢救（孤块补兜底册），盘上的规划文件一个字节都不改。
2. **内容结构兜底（无规划时）**：按块标题里的**章节标记**（`第3章` / `第三部分` / `第 12 讲`）
   把连续同标记的块归为一册，册名取该组实际标题；没有标记的块按顺序归为一组。
3. **体量兜底**：任何一册超过 `SIZE_CAP_BYTES` 时，在**块与块之间**再均衡切开
   （块绝不跨册拆开，册名加「（上）/（下）」或「（第N册）」）。

缺模块长文的块**不进书**（打 gate，不落占位册）；本轮不再产出的旧册会被清掉（教材是纯派生）。
命名：`模块<册号>_<册名>_精读全书.md`，册名来自规划或内容标题，不再是「第 N 册」这种空名。
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core import fsutil
from src.core.block_plan import BlockPlan
from src.core.heading_numbers import strip_heading_number
from src.core.workspace import find_module_article, sanitize_filename

# 一册的「块号 + 块 + 长文路径 + 字节数」四元组
_Item = Tuple[int, Dict[str, Any], Path, int]
# 分组：(册名, 该册的块序列)
_Group = Tuple[str, List[Dict[str, Any]]]


class PlanError(Exception):
    """盘上的 `textbook_plan.json` 存在但不可用（不是合法 JSON / 不是数组 / 元素全不合法）。

    与「没有规划文件」区分开：没文件是正常情况（走内容结构兜底），**坏文件不是**——
    以前坏文件被静默当成「没有规划」，Agent 写坏一个字符就悄悄退回兜底分册，
    用户完全不知道自己的规划没生效（第二阶段 A1）。
    """


class ArticleIntegrator:
    """按内容结构把块长文整编成册（册=书，章=块）。"""

    # 单册体量上限：整编是纯工具动作（不吃 token），这里只为**读者**分册——
    # 一本几 MB 的书在阅读器里翻不动。超限时按块边界均衡切册。
    SIZE_CAP_BYTES = 300 * 1024
    SUFFIX = "_精读全书.md"

    PLAN_NAME = "textbook_plan.json"
    PLAN_TASK_NAME = "textbook_plan_TASK.md"
    STATUS_PENDING_PLAN = "need-agent-textbook-plan"

    # 章节标记：`第3章` / `第三部分` / `第 12 讲` —— 块标题里带这种标记的，按标记成册
    _CHAPTER_MARK_RE = re.compile(
        r"第\s*([0-9０-９一二三四五六七八九十百]+)\s*(章|部分|讲|节|课)"
    )
    # 空泛册名（禁用作册名）：看不出内容的名字
    _VAGUE_TITLES = ("第一部分", "第二部分", "第三部分", "综合模块", "其他", "杂项", "未分类")

    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.articles_dir = self.task_dir / "articles"
        self.textbooks_dir = self.task_dir / "textbooks"
        self.textbooks_dir.mkdir(parents=True, exist_ok=True)

    def load_blocks(self) -> List[Dict[str, Any]]:
        """从工作区根目录的 v4 `block_plan.json` 读取块；没有块计划就没有教材可整编。"""
        return BlockPlan.load_blocks(self.task_dir)

    def plan_path(self) -> Path:
        return self.task_dir / self.PLAN_NAME

    @classmethod
    def _chapter_title(cls, block: Dict[str, Any]) -> str:
        """章标题 = 块标题（缺标题时退化为覆盖范围）。"""
        title = str(block.get("title") or "").strip()
        return title or BlockPlan.span(block).strip() or "未命名模块"

    # ------------------------------------------------------------------
    # 分册一：Agent 规划（首选）
    # ------------------------------------------------------------------
    def load_plan(self) -> List[Dict[str, Any]]:
        """读 `textbook_plan.json`。

        文件**不存在**时返回空表（调用方走内容结构兜底）；文件存在却读不出任何一册时抛
        `PlanError`——坏文件必须报出来，不能静默降级成「没有规划」。
        """
        path = self.plan_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanError(f"{path.name} 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, list):
            raise PlanError(
                f"{path.name} 必须是 JSON Array（每册一个对象），实际是 {type(raw).__name__}"
            )
        volumes = [v for v in raw if isinstance(v, dict)]
        if raw and not volumes:
            raise PlanError(
                f"{path.name} 里没有一个是合法分册对象"
                f"（每个元素都要是 {{\"volume_id\": 1, \"volume_title\": \"…\", \"blocks\": [1, 2]}}）"
            )
        return volumes

    @classmethod
    def validate_plan(
        cls, plan: Sequence[Dict[str, Any]], blocks: Sequence[Dict[str, Any]]
    ) -> Tuple[bool, str]:
        """校验分册规划：块**恰好被认领一次**、册名非空且能看出内容、`volume_id` 合法且唯一。"""
        if not plan:
            return False, "分册规划为空"
        known = [int(b.get("block_id") or 0) for b in blocks]
        claimed: List[int] = []
        ids: List[int] = []
        for index, volume in enumerate(plan, 1):
            title = str(volume.get("volume_title") or "").strip()
            if not title:
                return False, f"第 {index} 册缺少 volume_title（册名要能看出内容）"
            if title in cls._VAGUE_TITLES:
                return False, f"第 {index} 册用了空泛册名「{title}」，请写成能看出内容的主题名"
            raw_id = volume.get("volume_id")
            if raw_id is not None:
                try:
                    vid = int(raw_id)
                except (TypeError, ValueError):
                    return False, f"第 {index} 册的 volume_id 非法：{raw_id!r}（应为从 1 开始的整数）"
                if vid < 1:
                    return False, f"第 {index} 册的 volume_id 必须 >= 1，收到 {vid}"
                ids.append(vid)
            raw = volume.get("blocks")
            if not isinstance(raw, list) or not raw:
                return False, f"第 {index} 册未认领任何块"
            for item in raw:
                try:
                    claimed.append(int(item))
                except (TypeError, ValueError):
                    return False, f"第 {index} 册的块编号非法：{item!r}"
        dup_ids = sorted({v for v in ids if ids.count(v) > 1})
        if dup_ids:
            return False, f"volume_id 重复：{dup_ids}（每册的 volume_id 必须唯一）"
        unknown = sorted({b for b in claimed if b not in known})
        if unknown:
            return False, f"分册规划引用了不存在的块编号: {unknown}"
        duplicated = sorted({b for b in claimed if claimed.count(b) > 1})
        if duplicated:
            return False, f"同一块被多册重复认领: {duplicated}"
        missing = sorted(set(known) - set(claimed))
        if missing:
            return False, f"未被任何册认领的块: {missing}"
        return True, f"分册规划有效（{len(plan)} 册 / {len(blocks)} 个块）"

    @classmethod
    def salvage_plan(
        cls, plan: Sequence[Dict[str, Any]], blocks: Sequence[Dict[str, Any]]
    ) -> Tuple[List[_Group], List[str]]:
        """把不完全合法的分册规划抢救成全覆盖：先到先得 + 孤块各补一册。

        抢救结果只在内存里用，**不落盘**（盘上那份留给 Agent 修）。
        """
        by_id = {int(b.get("block_id") or 0): b for b in blocks}
        groups: List[_Group] = []
        diag: List[str] = []
        claimed: set = set()
        owner: Dict[int, int] = {}  # 块号 -> 先认领它的册序号（用于把「谁吞了谁」说清楚）
        unknown: List[Any] = []
        for vol_index, volume in enumerate(plan, 1):
            if not isinstance(volume, dict):
                continue
            picked: List[Dict[str, Any]] = []
            for raw in volume.get("blocks") or []:
                try:
                    bid = int(raw)
                except (TypeError, ValueError):
                    unknown.append(raw)
                    continue
                if bid not in by_id:
                    unknown.append(bid)
                    continue
                if bid in claimed:
                    # 静默丢弃会让用户完全无从知道哪一块被吞了（第二阶段 A2）。
                    if owner[bid] == vol_index:
                        diag.append(
                            f"块 {bid:02d} 在同一册（第 {vol_index} 册）里出现多次，已去重"
                        )
                    else:
                        diag.append(
                            f"块 {bid:02d} 被第 {vol_index} 册重复认领，已丢弃"
                            f"（保留先到者：第 {owner[bid]} 册）"
                        )
                    continue
                claimed.add(bid)
                owner[bid] = vol_index
                picked.append(by_id[bid])
            if not picked:
                continue
            title = str(volume.get("volume_title") or "").strip() or cls._group_title(picked)
            groups.append((title, sorted(picked, key=lambda b: int(b.get("block_id") or 0))))
        orphan = [b for bid, b in by_id.items() if bid not in claimed]
        if orphan:
            orphan.sort(key=lambda b: int(b.get("block_id") or 0))
            groups.append((cls._group_title(orphan), orphan))
            diag.append("未被认领、已补成兜底册的块："
                        + "、".join(f"{int(b.get('block_id') or 0):02d}" for b in orphan))
        if unknown:
            diag.append(f"引用了不存在的块、已忽略：{sorted(set(map(str, unknown)))}")
        groups.sort(key=lambda g: int(g[1][0].get("block_id") or 0))
        return groups, diag

    def export_plan_task(self, course_title: str, blocks: Sequence[Dict[str, Any]]) -> Path:
        """导出分册规划任务书（供 Agent 按内容划分册并起名）。"""
        task_file = self.task_dir / self.PLAN_TASK_NAME
        rows = []
        for index, block in enumerate(blocks, 1):
            block_id = int(block.get("block_id") or index)
            article = find_module_article(self.articles_dir, block)
            theme = ""
            if article is not None:
                try:
                    with article.open("r", encoding="utf-8", errors="replace") as handle:
                        for _ in range(60):
                            line = handle.readline()
                            if not line:
                                break
                            if line.strip().startswith("# "):
                                theme = line.strip()[2:].strip()
                                break
                except OSError:
                    theme = ""
            rows.append(
                f"- 块 {block_id:02d}（{BlockPlan.span(block)}，"
                f"{float(block.get('duration_min') or 0.0):.0f} 分钟）：{self._chapter_title(block)}"
                + (f"　〔长文主题：{theme}〕" if theme else "")
            )
        content = (
            f"# 教材分册规划任务书（TEXTBOOK_PLAN_TASK）\n\n"
            f"> 状态：{self.STATUS_PENDING_PLAN} | 由宿主 Agent 按**实际内容**划分册并起名\n"
            f"> 用途：把各块的模块长文整编成书时，决定「分几册、每册叫什么、装哪些块」\n"
            f"> 课程：{course_title}（共 {len(blocks)} 块）\n\n"
            f"## 1. 落盘要求\n\n"
            f"- 目标文件：`{self.plan_path().as_posix()}`\n"
            f"- 必须为合法 JSON Array，元素形如 "
            f"`{{\"volume_id\": 1, \"volume_title\": \"数据库前导与关系模型基础\", \"blocks\": [1, 2, 3]}}`\n"
            f"- **blocks 必须恰好覆盖全部 {len(blocks)} 个块各一次**：不得遗漏、不得重复；\n"
            f"- `volume_title` 必须**能看出内容**（如「关系数据理论与范式」「SQL 与数据库编程」），\n"
            f"  严禁「第一部分」「综合模块」「其他」这类空名——它就是教材文件名；\n"
            f"- 分册依据是**知识体系**：同一章、同一技术栈、同一条学习路线归到一册；册数由内容决定，\n"
            f"  不要为了整齐硬凑；单册体量过大时工具会在块边界再切分并加序号；\n"
            f"- 缺规划**不会卡住流程**：工具会按块标题里的章节标记兜底分册，补齐规划后重跑即替换\n\n"
            f"## 2. 块清单（册的划分对象）\n\n"
            + "\n".join(rows)
            + "\n"
        )
        task_file.write_text(content, encoding="utf-8")
        return task_file

    # ------------------------------------------------------------------
    # 分册二/三：内容结构兜底 + 体量兜底
    # ------------------------------------------------------------------
    @classmethod
    def _group_title(cls, blocks: Sequence[Dict[str, Any]]) -> str:
        """兜底册名：首块标题；多块时缀「等 N 块」，至少让人看出装了什么。"""
        head = cls._chapter_title(blocks[0]) if blocks else "未命名"
        return head if len(blocks) <= 1 else f"{head} 等 {len(blocks)} 块"

    @classmethod
    def _mark_of(cls, block: Dict[str, Any]) -> str:
        """块标题里的章节标记（如 `3章` / `三部分`），没有则空串。"""
        matched = cls._CHAPTER_MARK_RE.search(cls._chapter_title(block))
        return f"{matched.group(1)}{matched.group(2)}" if matched else ""

    def _sections(self) -> Dict[int, str]:
        """集号 → 平台分节名（`parts.json` 的 `section_title`，如「数据库第三章」）。

        平台分节是**真实内容结构**（UP 主自己划的章），拿它当兜底分册依据比按体量切靠谱得多；
        缺文件/缺字段时返回空表，调用方继续往下一级兜底。
        """
        path = self.task_dir / "parts.json"
        if not path.exists():
            return {}
        try:
            parts = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parts, list):
            return {}
        out: Dict[int, str] = {}
        for part in parts:
            if not isinstance(part, dict) or part.get("page") is None:
                continue
            section = str(part.get("section_title") or "").strip()
            if section:
                out[int(part["page"])] = section
        return out

    def _block_section(self, block: Dict[str, Any], sections: Dict[int, str]) -> str:
        """块的平台分节名：块内所有集同属一节才作数（劈分腿、跨节块都不会误判）。"""
        pages = [int(p) for p in (block.get("episodes") or [])]
        names = {sections.get(page, "") for page in pages}
        return names.pop() if len(names) == 1 else ""

    def _fallback_groups(self, blocks: Sequence[Dict[str, Any]]) -> List[_Group]:
        """内容结构兜底分册：平台分节 → 章节标记 → 无标记相邻合并，连续同组者并成一册。

        为什么不用「按体量一刀切」当兜底：那样得到的册名只能是「第 1 册 / 第 2 册」，
        看不出内容——教材是给人读的，册名本身就是目录。
        """
        sections = self._sections()
        buckets: List[Tuple[str, List[Dict[str, Any]], bool]] = []   # (分组键, 成员, 键是否可直接当册名)
        for block in blocks:
            section = self._block_section(block, sections)
            key, name_ready = (section, True) if section else (self._mark_of(block), False)
            if buckets:
                last_key, members, _ready = buckets[-1]
                if key and key == last_key:
                    members.append(block)
                    continue
                if not key and not last_key:
                    members.append(block)          # 相邻的无标记块并在一起（如课程前导）
                    continue
            buckets.append((key, [block], name_ready))
        groups: List[_Group] = []
        for key, members, name_ready in buckets:
            title = key if (name_ready and key) else self._group_title(members)
            groups.append((title, members))
        return groups

    @classmethod
    def _clip_title(cls, title: str, limit: int = 48) -> str:
        """册名截断：优先在分隔符处断开，避免把词切成半截。

        截在半句上很扎眼（实测出现过「…移动端适配与 F」这种尾巴）：宁可短一截，
        也要断在「：」「、」「，」这些语义分隔处；找不到再用硬截。
        """
        clean = sanitize_filename(title, max_len=limit + 20).strip()
        if len(clean) <= limit:
            return clean
        head = clean[:limit]
        for separator in ("：", "、", "，", ",", "；", ";", " "):
            cut = head.rfind(separator)
            if cut >= limit // 2:
                return head[:cut].strip()
        return head.strip()

    @staticmethod
    def _part_label(index: int, count: int) -> str:
        """体量再切分的册名后缀：两册用上/下，三册用上/中/下，更多用第 N 册。"""
        if count == 2:
            return "（上）" if index == 1 else "（下）"
        if count == 3:
            return {1: "（上）", 2: "（中）", 3: "（下）"}[index]
        return f"（第{index}册）"

    @staticmethod
    def _split_by_cap(items: Sequence[_Item], cap: int) -> List[List[_Item]]:
        """把一册的块序列按**块边界**均衡切分，使每段不超过 `cap`。"""
        if not items:
            return []
        total = sum(item[3] for item in items)
        if total <= cap:
            return [list(items)]
        parts = max(2, -(-total // cap))
        target = total / parts
        groups: List[List[_Item]] = []
        current: List[_Item] = []
        acc = 0
        for item in items:
            size = item[3]
            if current and (acc + size > cap or acc >= target):
                groups.append(current)
                current, acc = [], 0
            current.append(item)
            acc += size
        if current:
            groups.append(current)
        return groups

    # ------------------------------------------------------------------
    # 整编
    # ------------------------------------------------------------------
    def run(
        self,
        course_title: str,
        force: bool = False,
        blocks: Optional[List[Dict[str, Any]]] = None,
        size_cap_bytes: Optional[int] = None,
        plan: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[Path]:
        """按内容结构整编教材，返回本次落盘的**册**列表。

        分册优先级：显式 `plan` → 盘上 `textbook_plan.json` → 章节标记兜底。
        缺规划时导出规划任务书（不阻塞），缺模块长文的块 gate 跳过。
        """
        blocks = list(blocks) if blocks else self.load_blocks()
        ready: List[_Item] = []
        for index, block in enumerate(blocks, 1):
            module_idx = int(block.get("block_id") or index)
            article = find_module_article(self.articles_dir, block)
            if article is None:
                print(f"    [gate] 块 {module_idx:02d} 尚无模块长文，不纳入教材"
                      f"（待 articles/模块{module_idx:02d}_*_精读长文.md 落盘后重跑本命令）")
                continue
            try:
                size = article.stat().st_size
            except OSError:
                size = 0
            ready.append((module_idx, block, article, size))
        if not ready:
            # 没有可纳入的块时**也要清理**陈旧分册：否则删掉最后一篇长文后，上一轮产出的
            # `模块01_…_精读全书.md` 会作为陈旧产物永远留在盘上（第二阶段 A8）。
            # expected 传空集 = 本轮一册都不产出，盘上所有旧册都是陈旧的。
            self._remove_stale_volumes(set())
            return []

        # ---- 分册：Agent 规划优先 ----
        planned = list(plan) if plan is not None else self.load_plan()
        groups: List[_Group]
        if planned:
            ok, reason = self.validate_plan(planned, blocks)
            if ok:
                # 册的顺序**只认数组位置**。以前这里用 `volume_id` 建字典、又用位置下标去读
                # （`selection[index]`），于是 volume_id 写成 10/20 就 `KeyError`、
                # 写成乱序就静默错配到别的册（第二阶段 A1）。二者只留一种：位置。
                groups = []
                for volume in planned:
                    wanted = {int(x) for x in volume.get("blocks") or []}
                    members = sorted(
                        (b for b in blocks if int(b.get("block_id") or 0) in wanted),
                        key=lambda b: int(b.get("block_id") or 0),
                    )
                    groups.append((str(volume.get("volume_title") or "").strip(), members))
                print(f"    [plan] 按 {self.PLAN_NAME} 分 {len(groups)} 册")
            else:
                groups, diag = self.salvage_plan(planned, blocks)
                print(f"    [!] 盘上的分册规划不完全合法（{reason}），已就地抢救后继续：")
                for line in diag:
                    print(f"        - {line}")
                print(f"    [*] 盘上的 {self.PLAN_NAME} 未被改动；补齐后重跑即可替换。")
        else:
            groups = self._fallback_groups(blocks)
            task_file = self.export_plan_task(course_title, blocks)
            print(f"    [i] 未找到 {self.PLAN_NAME}：本轮按**块标题里的章节标记**兜底分册"
                  f"（{len(groups)} 册）；让 Agent 按内容划分册并起名后重跑即替换")
            print(f"    [i] 分册规划任务书：{task_file.name}")

        # ---- 体量兜底：超限的册在块边界再切 ----
        cap = int(size_cap_bytes or self.SIZE_CAP_BYTES)
        item_by_block = {item[0]: item for item in ready}
        volumes: List[Tuple[str, List[_Item]]] = []
        for title, members in groups:
            items = [item_by_block[int(b.get("block_id") or 0)] for b in members
                     if int(b.get("block_id") or 0) in item_by_block]
            if not items:
                continue
            parts = self._split_by_cap(items, cap)
            for part_index, part in enumerate(parts, 1):
                suffix = self._part_label(part_index, len(parts)) if len(parts) > 1 else ""
                volumes.append((f"{self._clip_title(title)}{suffix}", part))

        results: List[Path] = []
        expected: set = set()
        all_titles = [title for title, _ in volumes]
        for volume_index, (title, items) in enumerate(volumes, 1):
            path = self._write_volume(
                volume_index, all_titles, items, course_title, cap, force=force
            )
            results.append(path)
            expected.add(path.name)
        self._remove_stale_volumes(expected)
        return results

    def _write_volume(
        self,
        volume_index: int,
        volume_titles: Sequence[str],
        volume: Sequence[_Item],
        course_title: str,
        cap: int,
        force: bool = False,
        min_product_bytes: int = fsutil.RENDER_MIN_BYTES,
    ) -> Path:
        """写出一册：册名 → 导读与全景目录 → 逐章正文 → 册尾小结。"""
        volume_count = len(volume_titles)
        book_title = volume_titles[volume_index - 1]
        out_path = self.textbooks_dir / f"模块{volume_index:02d}_{book_title}{self.SUFFIX}"

        try:
            if out_path.exists() and out_path.stat().st_size >= min_product_bytes and not force:
                print(f"    [cached] 教材已存在，跳过整编: {out_path.name}")
                return out_path
        except OSError:
            pass

        volume_bytes = sum(item[3] for item in volume)
        if volume_bytes > cap:
            print(f"    [i] {out_path.name} 体量 {volume_bytes:,} 字节，超过上限 {cap:,}"
                  f"（该册内含单篇就超过上限的长文，已切无可切）")

        first_span = BlockPlan.span(volume[0][1])
        last_span = BlockPlan.span(volume[-1][1])
        # 讲数按**去重集号**统计：劈分腿会让同一集在两个块里各出现一次，直接相加会虚报
        episodes = {int(p) for _, b, _, _ in volume for p in (b.get("episodes") or [])}
        pages = len(episodes)
        minutes = sum(float(b.get("duration_min") or 0.0) for _, b, _, _ in volume)
        titles = self._display_titles(volume)

        # 目录用有序列表：序号是**目录序号**（渲染器不会给列表项自动编号），与「标题不写序号」不冲突。
        # 范围后缀只在标题里还没有时才补——消歧已经在标题里加过一次，再加就成「（P06上）（P06上）」。
        toc = []
        for i, (_, block, _, _) in enumerate(volume, 1):
            span = BlockPlan.span(block)
            marked = f"（{span}）" if span else ""
            toc.append(f"{i}. {titles[i - 1]}" + ("" if marked and marked in titles[i - 1] else marked))

        lines = [
            f"# {course_title}·{book_title}"
            + (f"（第 {volume_index} 册 / 共 {volume_count} 册）" if volume_count > 1 else ""),
            "",
            f"> **所属课程**：{course_title}  ",
            f"> **本册内容**：{book_title}  ",
            f"> **覆盖范围**：{first_span} ~ {last_span}"
            f"（共 {pages} 讲 / {len(volume)} 章 / {minutes:.0f} 分钟音频）  ",
        ]
        if volume_count > 1:
            lines.append(f"> **分册说明**：全书按内容分 {volume_count} 册；本册为第 {volume_index} 册  ")
        lines += [
            "> **整编说明**：正文逐字保留各块模块长文，仅补导读、目录与章间过渡。",
            "",
            "---",
            "",
            "## 导读与全景目录",
            "",
            *toc,
            "",
            "---",
            "",
        ]

        for i, (module_idx, block, article, _size) in enumerate(volume, 1):
            title = titles[i - 1]
            span = BlockPlan.span(block)
            lines.append(f"## {title}")
            block_title = self._chapter_title(block)
            trace = f"> 对应块：BLK{module_idx:02d} | 覆盖分集：{span}"
            if block_title and block_title != title:
                trace += f" | 块标题（分集名）：《{block_title}》"
            lines.append(trace)
            lines.append("")
            lines.append(self._read_article_body(article))
            lines.append("")
            if i < len(volume):
                next_title = titles[i]
                lines.append(f"> **承前启后**：上一节讲完「{title}」，下一节接着讲「{next_title}」。")
                lines.append("")
            lines.append("---")
            lines.append("")

        lines += [
            "## 本册小结",
            "",
            f"本册主题为「{book_title}」，整编了 {len(volume)} 个模块（{first_span} ~ {last_span}，"
            f"共 {pages} 讲 / {minutes:.0f} 分钟音频）。",
            "建议配合 `notes/` 目录下的复习笔记复盘，需要查证细节时可直接回到本册正文。",
            "",
        ]
        out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return out_path

    @staticmethod
    def _article_title(article: Path) -> str:
        """长文 H1——写作者读完语料提炼的主题，比块标题（分集名）更适合当章标题。

        块标题是「块内分集名的语义组合」，单集成块时它就是**分集原名**（如「数据库第2章
        关系数据库 （上）」，带平台编号与全角空格）；长文 H1 才是这一章真正讲了什么
        （如「关系模型结构与数据完整性约束」）。章标题取后者，分集名退到「对应块」备注里。
        """
        try:
            with article.open("r", encoding="utf-8", errors="replace") as handle:
                for _ in range(60):
                    line = handle.readline()
                    if not line:
                        break
                    text = line.strip().lstrip("﻿")
                    if text.startswith("# "):
                        return text[2:].strip()
        except OSError:
            pass
        return ""

    @classmethod
    def _display_titles(cls, volume: Sequence[_Item]) -> List[str]:
        """册内章标题：长文 H1 优先、块标题兜底；同名时补覆盖范围消歧。

        为什么必须消歧：单集超长被劈成上/下两条腿时，两章可能同名（长文都叫同一个主题，
        或块标题就是同一个集名），照搬会让书里出现两个同名章、过渡句变成
        「上一节讲完 A，下一节接着讲 A」——读起来像坏了。加范围后缀即可区分；
        目录随后**按标题里是否已有该范围**决定要不要再补，绝不出现「（P06上）（P06上）」。
        """
        raw = [
            cls._article_title(article) or cls._chapter_title(block)
            for _, block, article, _ in volume
        ]
        counts = {title: raw.count(title) for title in set(raw)}
        out: List[str] = []
        for title, (_, block, _, _) in zip(raw, volume):
            if counts[title] > 1:
                span = BlockPlan.span(block).strip()
                out.append(f"{title}（{span}）" if span else title)
            else:
                out.append(title)
        return out

    def _remove_stale_volumes(self, expected: set) -> int:
        """清掉本轮不再产出的旧册（教材是纯派生，可随时重建）。"""
        removed = 0
        for stale in sorted(self.textbooks_dir.glob(f"*{self.SUFFIX}")):
            if stale.name in expected:
                continue
            try:
                stale.unlink()
                removed += 1
                print(f"    [i] 已清理不再产出的旧册: {stale.name}")
            except OSError:
                pass
        if removed:
            print(f"    [i] 共清理 {removed} 册旧教材（本轮整编结果已写入 {len(expected)} 册）")
        return removed

    @staticmethod
    def _read_article_body(article: Path) -> str:
        """读模块长文正文：剥掉抬头（H1 / 引用块 / 分隔线），并把章内标题降一级、剥掉手写序号。

        为什么必须剥抬头：本书的章标题由本块接管，长文的 H1 与元信息引用块留在正文里会
        与章标题打架（旧实现只剥一行，多行引用块的第二行 `>` 会残留成章首的孤立引用）。
        为什么必须降级：长文的 `##` 与本书的章标题同级，不降级会与章标题并列成两个同层标题。
        """
        # errors="replace"：个别长文混入非 UTF-8 字节时不让整次整编崩掉；
        # 去 BOM：带 BOM 的长文首行是 \ufeff# 篇名，不去会漏过抬头剥离与去号规则。
        text = article.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")

        # Normalize any unescaped literal \n in markdown text
        norm_lines = []
        in_c = False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                in_c = not in_c
                norm_lines.append(line)
            elif not in_c and r"\n" in line:
                norm_lines.extend(line.replace(r"\n", "\n").splitlines())
            else:
                norm_lines.append(line)
        text = "\n".join(norm_lines)

        head_lines = text.strip().splitlines()
        head_idx = 0
        while head_idx < len(head_lines):
            head_text = head_lines[head_idx].strip()
            if (
                not head_text
                or head_text.startswith(">")
                or re.match(r"^#\s", head_text)          # 长文 H1（篇名）：由章标题接管
                or re.fullmatch(r"-{3,}", head_text)      # 抬头与正文之间的分隔线
            ):
                head_idx += 1
                continue
            break
        body = "\n".join(head_lines[head_idx:])

        # 先剥号、再降级（##→###、###→####）：存量长文标题带 `## 2.1 …` 这类手写序号，
        # 不剥会与阅读器的自动编号叠成双号；新长文已由提示词要求不写序号，所以这一步幂等。
        # 围栏判定与上面的归一化循环保持同一口径（strip 后判定）：列表项内缩进的围栏
        # 也是围栏，漏认会让围栏内的 `##` 被误降级、状态在两循环间失步。
        demoted = []
        in_code = False
        for line in body.strip().splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
            if not in_code:
                line = strip_heading_number(line)
                if line.startswith("#### "):
                    line = "#" + line  # becomes #####
                elif line.startswith("### "):
                    line = "#" + line  # becomes ####
                elif line.startswith("## "):
                    line = "#" + line  # becomes ###
            demoted.append(line)
        return "\n".join(demoted)
