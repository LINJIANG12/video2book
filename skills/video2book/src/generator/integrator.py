#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module Textbook Integrator：把各块的**模块长文**按块序整编成册（`textbooks/`）。

模块边界**不需要规划**：音频装箱（`audio/_blocks/blocks.json`，每块 40–60 分钟）就是知识模块，
一块一篇模块长文。教材因此是「块长文按块序整编」出的**书**：

* **册 = 书，章 = 块**：默认整门课编成一册；一册体量超过上限（`SIZE_CAP_BYTES`）时，
  在**块与块之间**均衡切册（块绝不跨册拆开）。
* 每册包含：册名与课程信息 → 全景目录（本册各章 = 各块）→ 逐章正文 → 章间承前启后 → 册尾小结。
* 整编是**加壳**：正文逐字保留 `articles/` 里的模块长文（剥掉长文抬头、章内标题降一级并去号），
  原文一律不动；缺模块长文的块**不进书**（打 gate，不落占位册）。
* 教材是纯派生产物：本轮不再产出的旧册会被清掉（可随时用 `cluster-articles` 重建）。

命名：单册 `模块01_<课程短名>_精读全书.md`；多册 `模块01_<课程短名>（第1册）_精读全书.md`
（编号是**册号**，不再等于块号——块号只出现在目录与章内备注里）。
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core.heading_numbers import strip_heading_number
from src.core.workspace import find_module_article, sanitize_filename

# 一册的「块 + 长文 + 字节数」三元组
_Item = Tuple[int, Dict[str, Any], Path, int]


class ArticleIntegrator:
    """把块长文按块序整编成册（册=书，章=块）。"""

    # 单册体量上限：整编是纯工具动作（不吃 token），这里只为**读者**分册——
    # 一本几 MB 的书在阅读器里翻不动。超限时按块边界均衡切册。
    SIZE_CAP_BYTES = 300 * 1024
    SUFFIX = "_精读全书.md"

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

    # ------------------------------------------------------------------
    # 分册
    # ------------------------------------------------------------------
    @staticmethod
    def _group_volumes(items: Sequence[_Item], cap: int) -> List[List[_Item]]:
        """把「块 + 长文」序列按**块边界**均衡切成若干册。

        册数取 `ceil(总量 / cap)`（下界：再多就超限），再按该目标做均衡装填：
        「装不下必须切」（硬）＋「够一册的量就切」（软，避免出现 300KB + 10KB 的畸形册）。
        单块长文本身就超过上限时切无可切，该册原样保留、由调用方打诊断。
        """
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

    @staticmethod
    def _volume_stem(volume_index: int, volume_count: int, course_title: str) -> str:
        """册的主干名：`模块<册号>_<课程短名>[（第N册）]`（编号是册号，不是块号）。"""
        clean = sanitize_filename(course_title or "课程", max_len=40)
        suffix = f"（第{volume_index}册）" if volume_count > 1 else ""
        return f"模块{volume_index:02d}_{clean}{suffix}"

    # ------------------------------------------------------------------
    # 整编
    # ------------------------------------------------------------------
    def run(
        self,
        course_title: str,
        force: bool = False,
        blocks: Optional[List[Dict[str, Any]]] = None,
        size_cap_bytes: Optional[int] = None,
    ) -> List[Path]:
        """按块序整编教材，返回本次落盘的**册**列表。

        `force=False`（默认）时复用已存在的册；`size_cap_bytes` 供自检与调参使用。
        缺模块长文的块被 gate 跳过（不落占位册）；本轮不再产出的旧册会被清掉。
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
            return []

        cap = int(size_cap_bytes or self.SIZE_CAP_BYTES)
        volumes = self._group_volumes(ready, cap)
        results: List[Path] = []
        expected: set = set()
        for volume_index, volume in enumerate(volumes, 1):
            path = self._write_volume(
                volume_index, len(volumes), volume, course_title, cap, force=force
            )
            results.append(path)
            expected.add(path.name)
        self._remove_stale_volumes(expected)
        return results

    def _write_volume(
        self,
        volume_index: int,
        volume_count: int,
        volume: Sequence[_Item],
        course_title: str,
        cap: int,
        force: bool = False,
        min_product_bytes: int = 200,
    ) -> Path:
        """写出一册：册名 → 导读与全景目录 → 逐章正文 → 册尾小结。"""
        stem = self._volume_stem(volume_index, volume_count, course_title)
        out_path = self.textbooks_dir / f"{stem}{self.SUFFIX}"
        volume_title = f"{course_title} 精读全书" + (
            f"（第 {volume_index} 册 / 共 {volume_count} 册）" if volume_count > 1 else ""
        )

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

        first_span = str(volume[0][1].get("span") or "")
        last_span = str(volume[-1][1].get("span") or "")
        # 讲数按**去重集号**统计：劈分腿会让同一集在两个块里各出现一次，直接相加会虚报
        episodes = {int(p) for _, b, _, _ in volume for p in (b.get("episodes") or [])}
        pages = len(episodes)
        minutes = sum(float(b.get("duration_min") or 0.0) for _, b, _, _ in volume)
        titles = self._display_titles(volume)

        # 目录用有序列表：序号是**目录序号**（渲染器不会给列表项自动编号），与「标题不写序号」不冲突
        toc = [
            f"{i}. {titles[i - 1]}（{str(block.get('span') or '')}）"
            for i, (_, block, _, _) in enumerate(volume, 1)
        ]

        lines = [
            f"# {volume_title}",
            "",
            f"> **所属课程**：{course_title}  ",
            f"> **覆盖范围**：{first_span} ~ {last_span}"
            f"（共 {pages} 讲 / {len(volume)} 章 / {minutes:.0f} 分钟音频）  ",
        ]
        if volume_count > 1:
            lines.append(f"> **分册说明**：全书按块序整编，因体量分 {volume_count} 册；"
                         f"本册为第 {volume_index} 册  ")
        lines += [
            "> **整编说明**：正文逐字保留各块模块长文，仅补导读、目录与章间过渡；"
            "原长文同步保留于 `articles/` 供定向查阅。",
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
            span = str(block.get("span") or "")
            lines.append(f"## {title}")
            lines.append(f"> 对应块：BLK{module_idx:02d} | 覆盖分集：{span}")
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
            f"本册整编了《{course_title}》的 {len(volume)} 个模块（{first_span} ~ {last_span}，"
            f"共 {pages} 讲 / {minutes:.0f} 分钟音频）。",
            "建议配合 `notes/` 目录下的复习笔记复盘；模块长文原文保留在 `articles/`，"
            "需要查证细节时可直接回到原文。",
            "",
        ]
        out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return out_path

    @staticmethod
    def _chapter_title(block: Dict[str, Any]) -> str:
        """章标题 = 块标题（缺标题时退化为覆盖范围）。"""
        title = str(block.get("title") or "").strip()
        return title or str(block.get("span") or "").strip() or "未命名模块"

    @classmethod
    def _display_titles(cls, volume: Sequence[_Item]) -> List[str]:
        """册内章标题：同名时补覆盖范围消歧。

        为什么必须消歧：单集超长被劈成上/下两条腿时，两个块的块标题**就是同一个集名**
        （如「数据库第2章 关系数据库（下）」的 P06上/P06下），照搬会让书里出现两个同名章、
        过渡句变成「上一节讲完 A，下一节接着讲 A」——读起来像坏了。加范围后缀即可区分。
        """
        raw = [cls._chapter_title(block) for _, block, _, _ in volume]
        counts = {title: raw.count(title) for title in set(raw)}
        out: List[str] = []
        for title, (_, block, _, _) in zip(raw, volume):
            if counts[title] > 1:
                span = str(block.get("span") or "").strip()
                out.append(f"{title}（{span}）" if span else title)
            else:
                out.append(title)
        return out

    def _remove_stale_volumes(self, expected: set) -> int:
        """清掉本轮不再产出的旧册（教材是纯派生，可随时重建）。

        不清的后果实测过：一块一册时代留下的 31 本壳会与新整编出的书并存，
        读者分不清哪本是当前产物。
        """
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
