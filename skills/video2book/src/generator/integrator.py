#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module Textbook Integrator：把各块的**模块长文**整编成分册教材（`textbooks/`）。

模块边界**不再需要规划**：音频装箱（`audio/_blocks/blocks.json`，每块 40–60 分钟）就是知识模块，
一块一篇模块长文。因此教材就是「块长文按块序整编」：一部 `模块XX_<块标题>_精读全书.md`
对应一个块，编号、标题与块音频、块长文完全同源。

整编做的是**加壳**，不是改写：补上模块导读、全景目录、章内过渡与模块总结，正文逐字保留
`articles/` 里的模块长文（原文一律不动）。

体积上限随第一趟模块规划一并取消：旧链路里一个模块可能装下 20 集的语料（实测超 ~300KB
后子智能体产出质量断崖下滑），才需要按字节切分册；现在一册的语料恒为**一个块的长文**
（40–60 分钟音频的产出），那个失效模式已经不存在。
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.heading_numbers import strip_heading_number
from src.core.workspace import find_module_article, sanitize_filename


class ArticleIntegrator:
    """Consolidates each block's module article into one modular textbook volume."""

    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.articles_dir = self.task_dir / "articles"
        self.textbooks_dir = self.task_dir / "textbooks"
        self.textbooks_dir.mkdir(parents=True, exist_ok=True)

    def load_blocks(self) -> List[Dict[str, Any]]:
        """读块清单——模块的唯一来源（没有块清单就没有教材可整编）。"""
        from src.core.audio_merger import AudioMerger

        manifest = AudioMerger.load_manifest(self.task_dir) or {}
        blocks = manifest.get("blocks") if isinstance(manifest, dict) else None
        return [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []

    def integrate_module(
        self,
        module_idx: int,
        block: Dict[str, Any],
        course_title: str,
        force: bool = False,
        min_product_bytes: int = 200,
    ) -> Path:
        """把该块的模块长文整编成一册教材。

        成品已存在且非 force 时直接复用（与 SKILL §7.2「模块教材自动复用」一致），
        需要按最新章节重编时显式传 force=True（CLI：`cluster-articles --force`）。
        """
        block_title = str(block.get("title") or "").strip() or f"块 {module_idx:02d}"
        clean_name = sanitize_filename(block_title)
        out_filename = f"模块{module_idx:02d}_{clean_name}_精读全书.md"
        out_path = self.textbooks_dir / out_filename

        try:
            if out_path.exists() and out_path.stat().st_size >= min_product_bytes and not force:
                print(f"    [cached] 模块教材已存在，跳过整编: {out_filename}")
                return out_path
        except OSError:
            pass

        pages = [int(p) for p in (block.get("episodes") or [])]
        page_range = f"P{min(pages):02d} ~ P{max(pages):02d}" if pages else "（无集号）"
        span = str(block.get("span") or page_range)
        minutes = float(block.get("duration_min") or 0.0)

        article = find_module_article(self.articles_dir, block)

        # Header and TOC：目录只有一条（本册就一个块），序号由渲染器生成——正文标题一律不写序号，
        # 写了会与阅读器的自动编号叠成「1. 第 1 章」这种双号。
        lines = [
            f"# 模块 {module_idx:02d}：{block_title} 合辑教材",
            "",
            f"> **所属课程**：{course_title}  ",
            f"> **覆盖范围**：{span}（{len(pages)} 讲 / {minutes:.1f} 分钟音频，块 {module_idx:02d}）  ",
            f"> **内容定位**：模块化系统学习教材，融合核心机制、架构全景、代码解析与思考自测。  ",
            f"> **关联说明**：本模块的模块长文同步保留于 `articles/` 目录供定向查阅。",
            "",
            "---",
            "",
            "## 模块导读与全景目录",
            "",
            block_title,
            "",
            "---",
            "",
        ]

        lines.append(f"## {block_title}")
        lines.append(f"> 对应块：BLK{module_idx:02d} | 覆盖分集：{span} | 块标题：《{block_title}》")
        lines.append("")

        if article is None:
            lines.append("> ⚠️ 模块长文暂未生成，可在后续流水线中补充。")
        else:
            lines.append(self._read_article_body(article))

        lines.extend([
            "",
            "---",
            "",
            f"## 模块 {module_idx:02d} 全景总结与技术沉淀",
            "",
            f"本全书整编了「{block_title}」这一模块（{span}）的核心专题。",
            "建议读者在学完本册后，对照 `notes/` 目录下的笔记进行复盘与知识自测，"
            "巩固底层机理与工程实践能力。",
            "",
        ])

        out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return out_path

    @staticmethod
    def _read_article_body(article: Path) -> str:
        """读模块长文正文：剥掉抬头（H1 / 引用块 / 分隔线），并把章内标题降一级、剥掉手写序号。

        为什么必须剥抬头：教材的章标题由本模块接管，长文的 H1 与元信息引用块留在正文里会
        与章标题打架（旧实现只剥一行，多行引用块的第二行 `>` 会残留成章首的孤立引用）。
        为什么必须降级：长文的 `##` 与本册的章标题同级，不降级会与章标题并列成两个同层标题。
        """
        text = article.read_text(encoding="utf-8")

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
        demoted = []
        in_code = False
        for line in body.strip().splitlines():
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
        return "\n".join(demoted)

    def run(
        self,
        course_title: str,
        force: bool = False,
        blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Path]:
        """按块序整编全部模块教材。

        force=False（默认）时复用已存在的模块教材；force=True 时全部重新整编。
        `blocks` 缺省时读盘上的块清单（`audio/_blocks/blocks.json`）。
        """
        blocks = list(blocks) if blocks else self.load_blocks()
        results = []
        for idx, block in enumerate(blocks, 1):
            module_idx = int(block.get("block_id") or idx)
            results.append(
                self.integrate_module(module_idx, block, course_title, force=force)
            )
        return results
