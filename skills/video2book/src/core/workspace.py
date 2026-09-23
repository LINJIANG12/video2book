"""任务工作区管理器（按任务隔离输出目录）。

目录结构（产物根由 `src/core/paths.py` 统一解析，默认 = 容器根下的 output/）：
产物根/
└── <任务名>/
    ├── 音频目录/      # 提取的音频与切片文件
    ├── 笔记目录/      # 结构化笔记
    ├── 文章目录/      # 深度文章
    ├── 字幕目录/      # 字幕与转写文本
    ├── 分集缓存/      # 分集列表缓存
    └── 清单文件/      # 任务元数据
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from . import fsutil
from . import paths as _paths


# **相对路径基准**：取「产物根之父目录」，因为产物恒为它下面的 `output/`。
# 这样 `to_relative(<基准>/output/<task>/x)` 恒等于 `output/<task>/x`——正是历史清单的写法。
#
# 为什么不沿用 `home_root()`：那是个**尽力而为的容器根提示值**，没有容器信号时会兜底到
# 一个与产物根未必同盘、甚至未必同树的位置（实测 Windows 平台安装下会落到用户主目录）。
# 基准一旦跨盘，`Path.relative_to` 与 `os.path.relpath` 会**双双抛 ValueError**，最内层兜底
# 只能原样返回绝对路径——于是清单里存的不再是 `output/<task>/...` 而是绝对路径，清单失去可移植性。
# `products_root()` 本身已按「容器内 → <home>/output；否则 → <cwd>/output」正确解析，
# 其父目录天然就是这条基准；容器布局下它与 `home_root()` 相等，因此这次改动**零回归**。
#
# ⚠️ 命名历史遗留：下面把它叫作 `_仓库根目录` / `REPO_ROOT`，但它的语义是**相对路径基准**，
#    与 selfcheck 里的 `REPO_ROOT`（仓库根 = 插件单元）**不是一回事**，读代码时不要混淆。
_仓库根目录 = _paths.products_root().parent


class TaskWorkspace:
    """任务工作区：隔离每个任务的输出文件。"""

    非法字符正则 = re.compile(r'[\\/*?:"<>|\n\r\t]+')

    # 容器根（相对路径换算基准）
    仓库根目录: Path = _仓库根目录
    REPO_ROOT: Path = _仓库根目录
    # 语义更准确的别名
    HOME_ROOT: Path = _仓库根目录

    def __init__(self, task_name: str, base_dir: Union[str, Path, None] = None):
        """初始化工作区并确保子目录存在（base_dir 为空即产物根）。"""
        self._bind(self.sanitize_name(task_name), base_dir)

    def _bind(self, task_name: str, base_dir: Union[str, Path, None] = None) -> None:
        """按**原样**目录名绑定工作区（不再清洗），并确保子目录存在。

        为什么内部通道必须绕过清洗：`sanitize_name` 会 strip 掉结尾的 `_`，而磁盘上的工作区名
        可能就以 `_` 收尾（如 NLP 课的 `…实战项目_`）。`create()` 已经按规则算好了确切的名字，
        再清洗一次就会把它改短一个字符，于是命令又在旁边建一个空目录（实测 exit 2）。
        """
        self.task_name = task_name
        self.base_dir = _paths.resolve_base_dir(base_dir)
        self.root_dir = self.base_dir / self.task_name
        self.audio_dir = self.root_dir / "audio"
        self.notes_dir = self.root_dir / "notes"
        self.articles_dir = self.root_dir / "articles"
        self.subtitles_dir = self.root_dir / "subtitles"
        # 分集列表缓存路径
        self.parts_cache_path = self.root_dir / "parts.json"
        self.manifest_file = self.root_dir / "manifest.json"

        self.ensure_dirs()

    @property
    def wbi_keys_file(self) -> Path:
        """签名密钥文件路径（供上层透传给签名器做文件级缓存）。"""
        return self.base_dir / ".wbi_keys.json"

    @classmethod
    def sanitize_name(cls, raw_name: str) -> str:
        """清理非法文件系统字符并限制任务文件夹长度（最大 80 字符，防 Windows MAX_PATH 溢出）。"""
        清理后 = cls.非法字符正则.sub("_", raw_name)
        清理后 = re.sub(r"_+", "_", 清理后).strip(" ._-")
        return 清理后[:80] if 清理后 else "task_unnamed"

    @classmethod
    def sanitize_title(cls, title: str) -> str:
        """用于文件名的分集或稿件标题转义：不执行字符过滤式清洗（保留 C++、C#、1.1、括号等原样符号），
        仅替换操作系统硬性禁止的非法字符并规范化空白与长度。"""
        cleaned = cls.非法字符正则.sub("_", title)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
        return cleaned[:80] if cleaned else "part"

    def ensure_dirs(self):
        """确保任务根目录与各分类子目录存在。"""
        for 目录 in [self.root_dir, self.audio_dir, self.notes_dir, self.articles_dir, self.subtitles_dir]:
            目录.mkdir(parents=True, exist_ok=True)

    # 工作区名长度上限（防 Windows MAX_PATH 溢出）
    TASK_NAME_MAX = 80
    # 工作区**完整路径**的安全上限。Windows 默认 MAX_PATH=260，留出
    # `\articles\P001_xxx_精读文章.md` 这类子路径余量后取 200：产物根本身很深时，
    # 光靠「名字 ≤80」不够（实测根 33 字符没问题，根再深到 ~150 字符就会撑破上限）。
    TASK_PATH_MAX = 200

    @classmethod
    def new_task_name(cls, title: str, bvid: str = "", base_dir: Union[str, Path, None] = None) -> str:
        """推导「标题 + BV 号」的工作区名，**结果再做任何清洗都不会变**。

        为什么不能直接 `sanitize_name(f"{sanitize_name(title)}_{bvid}")`：`sanitize_name`
        会 strip 掉结尾的 `_`、空格与点，于是「标题以空格/下划线收尾」的课程（实测 NLP 课）
        推出来的名字与磁盘上的名字差一个尾字符，`__init__` 再清洗一次还会继续吃掉字符——
        最终命令在别处建一个空目录、对着它报「尚无任何语料」。这里把结尾清理做在**拼接
        之前**，拼完只保证不超长，名字就是它本身。

        `base_dir` 给定时再按 `TASK_PATH_MAX` 收紧一次，保证完整路径不撑破系统上限。
        """
        标题 = cls.非法字符正则.sub("_", str(title or ""))
        标题 = re.sub(r"\s+", " ", 标题).strip(" ._")
        后缀 = f"_{bvid}" if bvid else ""
        上限 = cls.TASK_NAME_MAX
        if base_dir is not None:
            上限 = min(上限, max(len(后缀) + 1, cls.TASK_PATH_MAX - len(str(Path(base_dir))) - 1))
        可用 = max(1, 上限 - len(后缀))
        名字 = f"{标题[:可用].strip(' ._')}{后缀}"
        return 名字 if 名字.strip(" ._") else "task_unnamed"

    @classmethod
    def create(
        cls,
        title: str,
        bvid: str = "",
        custom_name: Optional[str] = None,
        base_dir: Union[str, Path, None] = None,
        info_name: str = "",
    ) -> "TaskWorkspace":
        """工厂方法：按标题与稿件号搭建任务工作区。

        命名规则只有一条：**工作区名 = 清洗后的标题 + `_<bvid>`**，标题超长时先给 BV 号留位
        再截标题（BV 号是唯一标识，被截掉就再也找不回来了）。

        名字定下来之后，按**磁盘上的事实**决定用哪个目录，顺序如下：

        1. `custom_name` / `info_name`（上游已定位到的目录名）——显式指定，原样使用；
        2. 推导名本身在盘上且有料；
        3. 盘上有**唯一一个**同源目录（`_same_course`）——历史工作区名被截断过，反推不出来。

        以上都落空就按推导名新建——**不猜**：有歧义或证据不足时另建工作区，
        猜错会静默读写到别门课的产物上。
        """
        resolved_base = _paths.resolve_base_dir(base_dir)
        base_name = cls.new_task_name(title, bvid, base_dir=resolved_base) if bvid else cls.sanitize_name(title)

        explicit = str(custom_name or info_name or "").strip()
        if explicit:
            # 显式指定：盘上同名且有料就原样用（`sanitize_name` 会吃掉尾下划线，
            # 把已有目录名改短就会跑到旁边建新的——实测 `--task` 传 80 字符名即踩中）。
            if cls._populated(resolved_base / explicit):
                return cls._make(explicit, resolved_base)
            return cls._make(cls.sanitize_name(explicit) if custom_name else base_name, resolved_base)

        if cls._populated(resolved_base / base_name):
            return cls._make(base_name, resolved_base)

        existing = cls._find_existing_by_bvid(resolved_base, title or base_name)
        return cls._make(existing or base_name, resolved_base)

    # 同源判定所需的最短公共前缀（字符）。**只用来挡退化输入**（单字、空名），真正的
    # 误认防线是 `_find_existing_by_bvid` 的「唯一性」——多个候选一律放弃。实测把门槛提到
    # 8~24 都救不了「短标题」场景，反而让「甲课程导论」这种 5 字标题认不出自己的目录。
    NAME_PREFIX_MIN = 4

    @staticmethod
    def _name_title_part(name: str) -> str:
        """名字里的**标题部分**：去掉 `_BV…` 后缀（含被截断的 BV 前缀）与所有空白。

        不能用「只匹配完整 12 位 BV 号」的正则：磁盘上的名字可能把 BV 号截断了
        （实测吉林大学 `…_BV1P7b5z`），那样尾巴会留在标题部分里，同一门课两边永远比不齐。
        """
        return "".join(str(name).rsplit("_BV", 1)[0].split()).rstrip("_")

    @classmethod
    def _same_course(cls, name_a: str, name_b: str) -> bool:
        """两个名字是否属于同一门课：**标题部分互为前缀**（任一侧够长）。

        依据：工作区名与课程标题同源，历史截断只会砍掉尾巴，所以同源的两份名字必然共享
        一段很长的标题前缀。实测：同一门课的标题部分或全等、或一方是另一方前缀
        （NLP 67、黑马 80、浙大 36、吉林大学 22），不同课则连 8 个字符都对不齐。
        """
        a, b = cls._name_title_part(name_a), cls._name_title_part(name_b)
        if not a or not b:
            return False
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        return len(short) >= cls.NAME_PREFIX_MIN and long_.startswith(short)

    @classmethod
    def _find_existing_by_bvid(cls, base_dir: Path, title: str = "") -> Optional[str]:
        """按标题找回**已存在**的同源工作区目录名；无候选或有歧义时返回 None。

        只有一条规则：`_same_course`。**歧义即放弃**——两个候选都同源说明证据不足，
        宁可另建工作区，也不要在两门同抬头的课之间乱认。
        """
        if not title or not base_dir.is_dir():
            return None
        stem = cls._name_title_part(title)
        if len(stem) < cls.NAME_PREFIX_MIN:
            return None
        try:
            children = [p for p in sorted(base_dir.iterdir()) if p.is_dir() and cls._populated(p)]
        except OSError:
            return None
        hits = [p.name for p in children if cls._same_course(p.name, title)]
        return hits[0] if len(hits) == 1 else None

    @classmethod
    def _make(cls, task_name: str, base_dir: Path) -> "TaskWorkspace":
        """按**已定稿**的名字造工作区：绑定阶段不再清洗。

        必要性：`sanitize_name` 会 strip 掉结尾的 `_`，而磁盘上的工作区名可能就以 `_` 收尾
        （如 NLP 课的 `…实战项目_`）。名字来自磁盘时再清洗一次就会短一个字符，于是命令跑到
        旁边另建一个空目录、对着它报「尚无任何语料」（实测 exit 2）。
        """
        ws = cls.__new__(cls)
        ws._bind(task_name, base_dir)
        return ws

    @classmethod
    def _populated(cls, path: Path) -> bool:
        """该目录是否已有实质语料（长文、**可复用的**逐字稿或课程结构缓存），而不是刚建出来的空壳。"""
        try:
            if (path / "parts.json").exists():
                return True
            articles = path / "articles"
            if articles.is_dir() and any(
                f for f in articles.glob("P*_*.md") if not f.name.endswith("_TASK.md")
            ):
                return True
            # 「什么算可复用语料」只有 `is_reusable_transcript` 一处判定——`_populated`
            # 与门禁/派发必须给出同一个答案，否则又会出现「工作区被反复复用、却永远推不动」
            # 的僵尸工作区（第二阶段 A7）。
            # 通配用 `*_逐字稿.md` 而不只是 `P*`：块级稿 `BLKxx_*_逐字稿.md` 同样是真语料，
            # 只有块级稿的工作区不该被当成空壳而重建目录、把稿子丢在外面。
            subtitles = path / "subtitles"
            if subtitles.is_dir() and any(
                cls.is_reusable_transcript(f)
                for f in subtitles.glob(f"*{cls.TRANSCRIPT_SUFFIX}")
            ):
                return True
        except OSError:
            return False
        return False

    @classmethod
    def from_existing(cls, path: Union[str, Path]) -> "TaskWorkspace":
        """绑定一个**已存在**的工作区目录，不做名称清洗。

        必要原因：`create()` 产出的目录名是「清洗后的标题 + _BV号」，拼接后可能超过
        `sanitize_name` 的 80 字符上限（如吉林大学《微机原理与接口技术》工作区名长达 100+ 字符）。
        若用 `__init__` 重新清洗，会算出与实际目录不符的路径。此入口只做路径绑定，不创建目录。
        """
        目录 = Path(path)
        if not 目录.is_absolute():
            目录 = (Path.cwd() / str(path)).resolve()
        实例 = cls.__new__(cls)
        实例.task_name = 目录.name
        实例.base_dir = 目录.parent
        实例.root_dir = 目录
        实例.audio_dir = 目录 / "audio"
        实例.notes_dir = 目录 / "notes"
        实例.articles_dir = 目录 / "articles"
        实例.subtitles_dir = 目录 / "subtitles"
        实例.parts_cache_path = 目录 / "parts.json"
        实例.manifest_file = 目录 / "manifest.json"
        return 实例

    @classmethod
    def to_absolute(cls, path: Union[str, Path]) -> Path:
        """相对仓库根目录换算为绝对路径（绝对路径直接归一化返回）。"""
        路径 = Path(str(path))
        if 路径.is_absolute():
            return 路径.resolve()
        return (cls.REPO_ROOT / str(path)).resolve()

    @classmethod
    def to_relative(cls, path: Union[str, Path]) -> str:
        """绝对路径换算为相对仓库根目录的相对路径（便于入库与展示）。"""
        try:
            绝对 = Path(str(path))
            if not 绝对.is_absolute():
                绝对 = (Path.cwd() / str(path)).resolve()
            else:
                绝对 = 绝对.resolve()
            return 绝对.relative_to(cls.REPO_ROOT).as_posix()
        except Exception:
            try:
                return os.path.relpath(str(path), str(cls.REPO_ROOT)).replace(os.sep, "/")
            except Exception:
                return str(path).replace(os.sep, "/")

    @staticmethod
    def merge_parts(existing: Any, incoming: Any) -> List[Any]:
        """按 `page` 合并分集拓扑，返回排序后的列表（`incoming` 覆盖同 page 旧值）。

        必要原因：`pipeline --page N` / `--range A-B` 这类**局部运行**只处理选中分集，
        若直接用子集覆盖 `parts.json`，就会把「分集拓扑缓存」截断成那几集——接口被风控
        时的离线自愈会据此误判课程规模，`cli.py sync` 的 episode_total 也随之变小。

        例外：`media_kind`（作品类型）是**拓扑属性**，不是本次运行的产物。旧调用方
        （历史代码、旧格式条目）不携带它，若被 incoming 整个覆盖掉，已标记的图文作品
        会被静默降级成视频，重新进入听音与派发。因此这里对它做「旧值兜底」：
        incoming 未给该键时沿用已有值。
        """
        merged: Dict[Any, Any] = {}
        order: List[Any] = []

        def _吸收(数据: Any, 沿用已有: bool = False) -> None:
            if not isinstance(数据, list):
                return
            for 条目 in 数据:
                if not isinstance(条目, dict):
                    continue
                键 = 条目.get("page")
                if 键 is None:
                    continue
                if 键 not in merged:
                    order.append(键)
                if 沿用已有 and "media_kind" not in 条目:
                    旧值 = merged.get(键)
                    if isinstance(旧值, dict) and 旧值.get("media_kind"):
                        条目 = {**条目, "media_kind": 旧值["media_kind"]}
                merged[键] = 条目

        _吸收(existing)
        _吸收(incoming, 沿用已有=True)

        def 排序键(键: Any) -> tuple:
            return (0, 键, "") if isinstance(键, int) else (1, 0, str(键))

        return [merged[键] for 键 in sorted(order, key=排序键)]

    def save_parts(self, parts: Union[List[Any], Dict[str, Any]]) -> Path:
        """原子写入分集列表缓存。"""
        目标 = self.parts_cache_path
        目标.parent.mkdir(parents=True, exist_ok=True)
        临时 = 目标.with_suffix(f".tmp.{os.getpid()}")
        with open(临时, "w", encoding="utf-8") as 写:
            json.dump(parts, 写, ensure_ascii=False, indent=2)
            写.flush()
            os.fsync(写.fileno())
        os.replace(临时, 目标)
        return 目标

    def load_parts(self) -> List[Any]:
        """读取分集列表缓存（缺失或损坏时返回空列表）。"""
        目标 = self.parts_cache_path
        if not 目标.exists():
            return []
        try:
            with open(目标, "r", encoding="utf-8") as 读:
                数据 = json.load(读)
            if isinstance(数据, list):
                return 数据
            return []
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 块级逐字稿：命名、落盘与「什么算可复用语料」
    # ------------------------------------------------------------------

    # 逐字稿的正式后缀。门禁、回收、派发与 `_populated` 都以它认稿。
    TRANSCRIPT_SUFFIX = "_逐字稿.md"

    @classmethod
    def is_reusable_transcript(cls, path: Path) -> bool:
        """`subtitles/` 下的这个文件算不算**可复用语料**。

        这是唯一真源：`_populated`（这个工作区要不要复用）与门禁/派发（这份稿能不能当语料）
        必须给出同一个答案。历史 `PXX_*_clean.txt` 是旧链路的放行口——任何一段来路不明的
        文本顶着这个名字就能被喂进流水线（2026-09 伪逐字稿事故），所以**不算**。
        以前 `_populated` 却认它，于是工作区被反复复用却永远推不动（第二阶段 A7）。
        """
        try:
            return (
                path.name.endswith(cls.TRANSCRIPT_SUFFIX)
                and path.is_file()
                and path.stat().st_size > 0
            )
        except OSError:
            return False

    @classmethod
    def block_path(cls, ws: Any, block: Dict[str, Any]) -> Path:
        """块级原始逐字稿的路径。

        优先用「块号 + 覆盖范围」（`BLK03_P18-P22_逐字稿.md`，含劈分腿时为 `BLK03_P12上-P13_逐字稿.md`）：
        它只取决于清单里的块结构，与块音频落在哪无关。这一点在**无收益装箱**（每块仅一集、
        块音频直接指向该集原音频）时尤其重要——否则块级稿会跟历史分集稿同名（都成
        `P08_标题_逐字稿.md`）而互相覆盖。清单缺块号/覆盖范围时退回按音频文件名取名（兼容手写的旧清单）。
        """
        block_id = int(block.get("block_id") or 0)
        pages = sorted(int(p) for p in (block.get("episodes") or []))
        span = str(block.get("span") or "")
        if not span:
            labels = [str(u.get("label") or "") for u in (block.get("units") or []) if u.get("label")]
            span = labels[0] if len(labels) == 1 else (f"{labels[0]}-{labels[-1]}" if labels else "")
        if block_id and (span or pages):
            span = span or (f"P{pages[0]:02d}" if len(pages) == 1 else f"P{pages[0]:02d}-P{pages[-1]:02d}")
            return Path(ws.subtitles_dir) / f"BLK{block_id:02d}_{span}{cls.TRANSCRIPT_SUFFIX}"
        stem = Path(str(block.get("audio") or "")).stem or f"BLK{block_id:02d}"
        return Path(ws.subtitles_dir) / f"{stem}{cls.TRANSCRIPT_SUFFIX}"

    @classmethod
    def write_block_transcript(cls, ws: Any, block: Dict[str, Any], text: str) -> Path:
        """落块级原始逐字稿。它是可溯源的原始事实，无论来路（听音转录或 B 站字幕）。"""
        path = cls.block_path(ws, block)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # 单值路径字段（策略：入库相对仓库根，读回绝对路径）
    PATH_KEYS = {
        "audio", "transcript", "task_prompt", "task_file", "article",
        "audio_file", "filepath", "source_path", "target_path", "chunk_path", "chunk_file",
        # 模块笔记任务书结果里的目标文件；缺了它会把机器绝对路径写进 manifest
        "note_file", "kernel_file",
        # 块清单路径（audio/_blocks/blocks.json）；不登记会把机器绝对路径留在 manifest 里
        "blocks_manifest",
    }

    # 列表型路径字段（元素为路径字符串）：textbooks / notes_files / kernels 等
    # 只在字典分支按 key 判定，因此必须单独列一份并在两个方向都逐项转换，
    # 否则 cluster-articles 单独跑完会把盘符绝对路径留在 manifest 里。
    PATH_LIST_KEYS = {"textbooks", "notes_files", "kernels"}

    @classmethod
    def _convert_path_list(cls, values: Any, converter, fallback) -> Any:
        """对列表型路径字段逐项做路径换算；非列表原样交给 fallback 递归处理。"""
        if not isinstance(values, list):
            return fallback(values)
        return [converter(v) if isinstance(v, (str, Path)) else fallback(v) for v in values]

    @classmethod
    def relativize_obj(cls, obj: Any) -> Any:
        """递归将字典/列表中属于路径键的值换算为相对仓库根目录的相对路径。"""
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k in cls.PATH_LIST_KEYS:
                    result[k] = cls._convert_path_list(v, cls.to_relative, cls.relativize_obj)
                elif k in cls.PATH_KEYS and isinstance(v, (str, Path)):
                    result[k] = cls.to_relative(v)
                else:
                    result[k] = cls.relativize_obj(v)
            return result
        if isinstance(obj, list):
            return [cls.relativize_obj(x) for x in obj]
        return obj

    @classmethod
    def absolutize_obj(cls, obj: Any) -> Any:
        """递归将字典/列表中属于路径键的值换算为绝对路径供程序内部安全读取。"""
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k in cls.PATH_LIST_KEYS:
                    result[k] = cls._convert_path_list(
                        v, lambda p: str(cls.to_absolute(p)), cls.absolutize_obj
                    )
                elif k in cls.PATH_KEYS and isinstance(v, (str, Path)):
                    result[k] = str(cls.to_absolute(v))
                else:
                    result[k] = cls.absolutize_obj(v)
            return result
        if isinstance(obj, list):
            return [cls.absolutize_obj(x) for x in obj]
        return obj

    def save_manifest(self, data: Dict[str, Any], relative: bool = True):
        """持久化或增量更新任务清单（原子写入，默认转换为相对路径保持跨环境便携）。"""
        已有 = {}
        if self.manifest_file.exists():
            try:
                with open(self.manifest_file, "r", encoding="utf-8") as 读:
                    已有 = json.load(读)
            except Exception:
                已有 = {}

        payload = self.relativize_obj(data) if relative else data
        已有.update(payload)
        临时 = self.manifest_file.with_suffix(f".tmp.{os.getpid()}")
        with open(临时, "w", encoding="utf-8") as 写:
            json.dump(已有, 写, ensure_ascii=False, indent=2)
            写.flush()
            os.fsync(写.fileno())
        os.replace(临时, self.manifest_file)

    def load_manifest(self, absolute: bool = False) -> Dict[str, Any]:
        """读取清单，不存在或损坏时返回空字典；可选项转为绝对路径。"""
        if not self.manifest_file.exists():
            return {}
        try:
            with open(self.manifest_file, "r", encoding="utf-8") as 读:
                data = json.load(读)
            if absolute:
                return self.absolutize_obj(data)
            return data
        except Exception:
            return {}

    @staticmethod
    def compute_file_hash(filepath: Union[str, Path]) -> str:
        """计算指定文件的 SHA-256 摘要哈希。"""
        import hashlib
        p = Path(filepath)
        if not p.is_file():
            return ""
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()



MODULE_ARTICLE_SUFFIX = "_精读长文.md"


def module_article_stem(block: Dict[str, Any]) -> str:
    """模块长文与任务书的主干名：`模块XX_<语义组合标题>`（标题来自块清单）。

    这是块级链路对「块 → 交付物」命名的**唯一来源**：工具、队列、对账、门禁、阶段二都调它，
    避免各处各写一套正则与拼接规则（旧链路正是这样散开后才出现口径漂移）。
    """
    block_id = int(block.get("block_id") or 0)
    title = str(block.get("title") or "").strip() or str(block.get("span") or f"BLK{block_id:02d}")
    return f"模块{block_id:02d}_{sanitize_filename(title, max_len=40)}"


def module_article_path(articles_dir: Union[str, Path], block: Dict[str, Any]) -> Path:
    """块对应的模块长文正式路径（`articles/模块XX_<标题>_精读长文.md`）。"""
    return Path(articles_dir) / f"{module_article_stem(block)}{MODULE_ARTICLE_SUFFIX}"


def module_task_path(articles_dir: Union[str, Path], block: Dict[str, Any]) -> Path:
    """块对应的模块长文任务书路径（`articles/模块XX_<标题>_TASK.md`）。"""
    return Path(articles_dir) / f"{module_article_stem(block)}_TASK.md"


def find_module_article(
    articles_dir: Union[str, Path], block: Dict[str, Any], min_bytes: int = fsutil.PRODUCT_MIN_BYTES
) -> Optional[Path]:
    """磁盘上已就绪的模块长文（按 `模块XX_` 前缀宽容定位，容忍标题微调与后缀差异）。"""
    root = Path(articles_dir)
    if not root.is_dir():
        return None
    block_id = int(block.get("block_id") or 0)
    for candidate in sorted(root.glob(f"模块{block_id:02d}_*.md")):
        if candidate.name.endswith("_TASK.md"):
            continue
        try:
            if candidate.stat().st_size >= min_bytes:
                return candidate
        except OSError:
            continue
    return None


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清理用于文件名的分集或稿件标题（模块级快捷函数）。"""
    return TaskWorkspace.sanitize_title(name)[:max_len]


_EPISODE_AUDIO_RE = re.compile(r"^P(\d+)[_.](.+)\.(?:m4a|mp3|wav|aac|flac)$", re.IGNORECASE)


def parts_from_audio(dir_path: Path) -> List[Dict[str, Any]]:
    """从 audio/ 的分集文件名反推集号与标题（离线自愈的最后一档）。"""
    audio_dir = Path(dir_path) / "audio"
    if not audio_dir.exists():
        return []
    parts: Dict[int, Dict[str, Any]] = {}
    for entry in sorted(audio_dir.glob("P*")):
        if not entry.is_file():
            continue
        matched = _EPISODE_AUDIO_RE.match(entry.name)
        if not matched:
            continue
        page = int(matched.group(1))
        parts.setdefault(page, {"page": page, "title": matched.group(2).strip()})
    return [parts[k] for k in sorted(parts)]


def offline_candidate_dirs(
    out_base: Path,
    bvid: str,
    custom_task: Optional[str] = None,
    season_id: Optional[Any] = None,
) -> List[Path]:
    """接口受阻时按 BV 号找回本地工作区目录（离线自愈的定位入口）。"""
    def _has_content(path: Path) -> bool:
        return (path / "parts.json").exists() or bool(parts_from_audio(path))

    cands: List[Path] = []
    out_base = Path(out_base)
    if custom_task:
        cands.append(out_base / TaskWorkspace.sanitize_name(custom_task))
    if out_base.exists():
        found = [p for p in out_base.glob(f"*{bvid}*") if fsutil.is_dir(p)]
        if not found and len(bvid) > 6:
            found = [p for p in out_base.glob(f"*{bvid[:6]}*") if fsutil.is_dir(p) and _has_content(p)]
        cands.extend(found)
        if bvid or season_id:
            known = {str(p.resolve()).lower() for p in cands}
            for candidate in fsutil.iter_child_dirs(out_base):
                if str(candidate.resolve()).lower() in known or not _has_content(candidate):
                    continue
                parts_file = candidate / "parts.json"
                if not parts_file.exists():
                    continue
                try:
                    cached = json.loads(parts_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(cached, list):
                    continue
                for item in cached:
                    if not isinstance(item, dict):
                        continue
                    item_bvid = str(item.get("bvid") or "")
                    item_season = str(item.get("season_id") or "")
                    if ((bvid and item_bvid.lower() == bvid.lower())
                            or (season_id and item_season == str(season_id))):
                        cands.append(candidate)
                        break
    return cands


def workspace_title(dir_path: Path, manifest: Optional[Dict[str, Any]] = None) -> str:
    """离线自愈时的课程标题：目录名优先，manifest 的 title 只作兜底。"""
    dir_path = Path(dir_path)
    derived = dir_path.name.split("_")[0].strip()
    if derived:
        return derived
    return str((manifest or {}).get("title") or "").strip() or dir_path.name


_offline_candidate_dirs = offline_candidate_dirs
_workspace_title = workspace_title
_parts_from_audio = parts_from_audio

