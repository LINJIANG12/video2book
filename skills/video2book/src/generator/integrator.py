#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module Textbook Integrator.

Integrates single-episode articles in `articles/` into unified modular textbooks in `textbooks/`.
Original articles in `articles/` are strictly preserved.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from src.core.heading_numbers import strip_heading_number
from src.core.workspace import sanitize_filename


class ArticleIntegrator:
    """Consolidates individual episode articles into comprehensive modular chapter textbooks."""

    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.articles_dir = self.task_dir / "articles"
        self.textbooks_dir = self.task_dir / "textbooks"
        self.parts_file = self.task_dir / "parts.json"
        self.textbooks_dir.mkdir(parents=True, exist_ok=True)

    def load_parts(self) -> List[dict]:
        if self.parts_file.exists():
            try:
                return json.loads(self.parts_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Fallback 1: manifest.json details or episodes
        manifest_file = self.task_dir / "manifest.json"
        if manifest_file.exists():
            try:
                m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                details = m_data.get("details", [])
                if details and len(details) > 1:
                    return [{"page": d["page"], "title": d.get("title", f"P{d['page']:02d}")} for d in details if "page" in d]
            except Exception:
                pass

        # Fallback 2: parse articles directory
        parts = []
        if self.articles_dir.exists():
            for f in sorted(self.articles_dir.glob("P*_精读文章.md")):
                m = re.match(r"P(\d+)_(.*?)_精读文章\.md", f.name)
                if m:
                    parts.append({"page": int(m.group(1)), "title": m.group(2).strip()})
        if parts:
            return parts

        # Fallback 3: topic_plan.json or manifest knowledge_blocks_plan
        plan = []
        topic_plan_file = self.task_dir / "topic_plan.json"
        if topic_plan_file.exists():
            try:
                plan = json.loads(topic_plan_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        elif manifest_file.exists():
            try:
                m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                plan = m_data.get("knowledge_blocks_plan", [])
            except Exception:
                pass

        all_eps = set()
        for b in plan:
            for ep in b.get("episodes", []):
                all_eps.add(ep)
        if all_eps:
            # 只认**磁盘上真有长文**的集号：规划可能是越界的旧版本（如工作区只有 P01–P87
            # 而规划按 P01–P185 写的），照单全收会整出几十章「长文暂未生成」的空教材。
            on_disk = {
                int(re.match(r"P(\d+)_", f.name).group(1))
                for f in self.articles_dir.glob("P*_*.md")
                if not f.name.endswith("_TASK.md") and re.match(r"P(\d+)_", f.name)
            }
            usable = sorted(all_eps & on_disk) if on_disk else sorted(all_eps)
            if usable:
                return [{"page": ep, "title": f"第{ep}讲"} for ep in usable]

        return []

    def group_episodes_by_module(
        self, parts: List[dict], plan: Optional[List[dict]] = None
    ) -> Dict[str, List[dict]]:
        """Groups parts into distinct logical modules based on title semantics.

        `plan` 显式传入时以它为准：调用方（CLI）已按**语料体积上限**归一过模块边界
        （这是**教材分册专用**的归一，笔记侧不做体积切分）。缺省时保持原行为——先读盘上的
        `topic_plan.json`（或 manifest 的 `knowledge_blocks_plan`），再退回按标题章节号 / 序号前缀分组。
        """
        modules: Dict[str, List[dict]] = {}
        if plan is None:
            # First try to load from knowledge_blocks_plan if present in manifest.json or topic_plan.json
            manifest_file = self.task_dir / "manifest.json"
            topic_plan_file = self.task_dir / "topic_plan.json"
            plan = []
            if topic_plan_file.exists():
                try:
                    plan = json.loads(topic_plan_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if not plan and manifest_file.exists():
                try:
                    m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    plan = m_data.get("knowledge_blocks_plan", [])
                except Exception:
                    pass

        if plan:
            page_map = {p["page"]: p for p in parts}
            for b in plan:
                title = b.get("block_title", "知识模块")
                b_eps = [page_map[ep] for ep in b.get("episodes", []) if ep in page_map]
                if b_eps:
                    modules[title] = b_eps
            if modules:
                return modules

        # 通用回退分组：优先按 [X.Y] 章节号前缀，其次按标题序号前缀
        for p in parts:
            title = p.get("title", "")
            m_ch = re.search(r"\[(\d+)\.", title)
            if m_ch:
                module_key = f"第{int(m_ch.group(1)):02d}章"
            else:
                m_pref = re.match(r"\s*(\d+)[\.、\s]", title)
                module_key = f"第{int(m_pref.group(1)):02d}讲" if m_pref else "综合模块"

            if module_key not in modules:
                modules[module_key] = []
            modules[module_key].append(p)
        return modules

    def integrate_module(
        self,
        module_idx: int,
        module_name: str,
        episodes: List[dict],
        course_title: str,
        force: bool = False,
        min_product_bytes: int = 200,
    ) -> Path:
        """Compiles articles of a module into a single unified textbook.

        成品已存在且非 force 时直接复用（与 SKILL §7.2「模块教材自动复用」一致），
        需要按最新章节重编时显式传 force=True（CLI：`cluster-articles --force`）。
        """
        clean_name = sanitize_filename(module_name)
        out_filename = f"模块{module_idx:02d}_{clean_name}_精读全书.md"
        out_path = self.textbooks_dir / out_filename

        try:
            if out_path.exists() and out_path.stat().st_size >= min_product_bytes and not force:
                print(f"    [cached] 模块教材已存在，跳过整编: {out_filename}")
                return out_path
        except OSError:
            pass

        ep_pages = [ep["page"] for ep in episodes]
        page_range = f"P{min(ep_pages):02d} ~ P{max(ep_pages):02d}"

        # Header and TOC
        lines = [
            f"# 模块 {module_idx:02d}：{module_name} 合辑教材",
            "",
            f"> **所属课程**：{course_title}  ",
            f"> **模块跨度**：{page_range}（全模块共 {len(episodes)} 讲系统重构）  ",
            f"> **内容定位**：模块化系统学习教材，融合核心机制、架构全景、代码解析与思考自测。  ",
            f"> **关联说明**：单集长文讲义同步保留于 `articles/` 目录供定向查阅。",
            "",
            "---",
            "",
            "## 模块导读与全景目录",
            "",
        ]

        # TOC：用有序列表（序号由渲染器生成）。正文标题一律不写序号——写了会与阅读器的
        # 自动编号叠成「1. 第 1 章」这种双号，笔记侧已踩过同一个坑。
        for i, ep in enumerate(episodes, 1):
            title = ep.get("title", "")
            clean_t = re.sub(r"^\d+\.\s*", "", title)
            lines.append(f"{i}. {clean_t}")
        lines.extend(["", "---", ""])

        # Chapters
        for i, ep in enumerate(episodes, 1):
            page = ep["page"]
            title = ep.get("title", "")
            clean_t = re.sub(r"^\d+\.\s*", "", title)
            matches = list(self.articles_dir.glob(f"P{page:02d}_*_精读文章.md"))
            if not matches:
                matches = [f for f in self.articles_dir.glob(f"P{page:02d}_*.md") if not f.name.endswith("_TASK.md")]
            
            lines.append(f"## {clean_t}")
            lines.append(f"> 对应分集：P{page:02d} | 原始标题：《{title}》")
            lines.append("")

            if matches:
                art_content = matches[0].read_text(encoding="utf-8")
                # Normalize any unescaped literal \n in markdown text
                norm_lines = []
                in_c = False
                for l in art_content.splitlines():
                    if l.strip().startswith("```"):
                        in_c = not in_c
                        norm_lines.append(l)
                    elif not in_c and r"\n" in l:
                        norm_lines.extend(l.replace(r"\n", "\n").splitlines())
                    else:
                        norm_lines.append(l)
                art_content = "\n".join(norm_lines)

                # Strip the leading H1 + metadata quote block + separator (header only)：
                # 长文抬头可能是多行引用块，必须逐行剥离到第一行正文为止，
                # 否则第二行 `>` 会残留在教材章首（旧实现只剥一行）。
                head_lines = art_content.strip().splitlines()
                head_idx = 0
                while head_idx < len(head_lines):
                    head_text = head_lines[head_idx].strip()
                    if (
                        not head_text
                        or head_text.startswith(">")
                        or re.match(r"^#\s", head_text)      # 长文 H1（篇名）：由教材章标题接管
                        or re.fullmatch(r"-{3,}", head_text)  # 抬头与正文之间的分隔线
                    ):
                        head_idx += 1
                        continue
                    break
                art_content = "\n".join(head_lines[head_idx:])
                
                # 先剥号、再降级（##→###、###→####）：存量长文标题带 `## 2.1 …` 这类手写序号，
                # 不剥会与阅读器的自动编号叠成双号；新长文已由提示词要求不写序号，所以这一步幂等。
                demoted = []
                in_code = False
                for line in art_content.strip().splitlines():
                    if line.startswith("```"):
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
                lines.append("\n".join(demoted))
            else:
                lines.append(f"> ⚠️ 单集精读长文暂未生成，可在后续流水线中补充。")

            # Transition bridge if not last chapter
            if i < len(episodes):
                next_title = re.sub(r"^\d+\.\s*", "", episodes[i].get("title", ""))
                lines.extend([
                    "",
                    f"> 💡 **承前启后**：完成对「{clean_t}」的理解后，下一章我们将深入探讨「{next_title}」，进一步完善知识图谱体系。",
                    "",
                    "---",
                    "",
                ])
            else:
                lines.extend(["", "---", ""])

        # Module Summary section
        lines.extend([
            f"## 模块 {module_idx:02d} 全景总结与技术沉淀",
            "",
            f"本全书系统整合了 {module_name} 模块的 {len(episodes)} 个核心专题（{page_range}）。",
            "建议读者在学完本章后，对照 `notes/` 目录下的思维导图树状笔记进行复盘与知识自测，巩固底层机理与工程实践能力。",
            "",
        ])

        out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return out_path

    def run(
        self,
        course_title: str,
        force: bool = False,
        parts: Optional[List[dict]] = None,
        plan: Optional[List[dict]] = None,
    ) -> List[Path]:
        """Runs the complete module integration process.

        force=False（默认）时复用已存在的模块教材；force=True 时全部重新整编。
        `parts` 显式传入时以它为准（CLI 已按「工作区 parts.json 优先」解析过集号基准），
        免得这一层再去猜一遍工作区到底有哪几集。
        `plan` 显式传入时以它为准，且**不再读盘上的规划**：CLI 已按语料体积上限归一过
        模块边界（**教材分册专用**；笔记侧不做体积切分，一篇笔记与 `note_plan.json` 一条对应）。
        """
        parts = list(parts) if parts else self.load_parts()
        grouped = self.group_episodes_by_module(parts, plan=plan)
        results = []
        for idx, (mod_name, eps) in enumerate(grouped.items(), 1):
            path = self.integrate_module(idx, mod_name, eps, course_title, force=force)
            results.append(path)
        return results
