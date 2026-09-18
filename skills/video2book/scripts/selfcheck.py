#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal runnable self-check for the video2book skill repo (三域分离后的技能侧自检).

Not a test framework: a flat sequence of assertions covering the invariants that
matter after the architecture refactor (Agent-native kernel/plan chain, zero
intermediate transcript, no dead modules) **plus the three-domain separation
contract**: skill/ 与承载两个 MCP 的 omni-media/ 各自独立成仓、产物根在两者之外、
CLI 不依赖当前工作目录。

MCP 自身的不变量由 MCP 各自的 `selfcheck.py` 负责（`mcp/` 与 `mcp-ext/` 各一份）；
本脚本在能找到它们时以子进程方式调用（可选段落，缺失即跳过，技能侧不依赖 MCP）。

Run: python scripts/selfcheck.py
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 两个基准根（技能自包含后必须分开，否则一堆断言会指向错位置）：
#   SKILL_ROOT = skills/video2book/  ← SKILL.md / references/ / src/ / scripts/ 都在这里（= 安装单元）
#   REPO_ROOT  = 插件根（仓库根）          ← .git / .gitignore / README / pyproject / 平台声明在这里
SKILL_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_ROOT.parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from src.core import paths as _paths  # noqa: E402
from src.core.console import enable_utf8_console  # noqa: E402
from src.core.proc import run_quiet  # noqa: E402  （统一抑制 Windows 控制台窗口）

# 控制台硬化：自检输出含中文与 `[PASS]/[FAIL]`，管道捕获时若按 locale(cp936) 编码会崩。
enable_utf8_console()

# 容器根（skill/、omni-media/、output/ 的共同父目录）与产物根
HOME_ROOT = _paths.home_root()
PRODUCTS_ROOT = _paths.products_root()
# 两个 MCP 的位置**不在本文件里猜**：统一走 paths.py 的解析
# （含 $OMNI_MEDIA_MCP_DIR 覆盖、新布局优先与迁移前旧布局兜底）。
MCP_REPO = _paths.mcp_repo()
MCP_EXT_REPO = _paths.mcp_ext_repo()
# 承载两个 MCP 的仓库根（新布局为 <home>/omni-media）。迁移前两者各自独立成仓，
# 故下面的仓库边界断言对旧布局另有一条分支。
# 承载两个 MCP 的仓库根（新布局为 <base>/omni-media）。由**解析结果**反推而非拿 home_root() 拼：
# 平台安装下 home_root() 只是提示值，拼出来的位置可能根本不存在；实际探查到的才是真位置。
MCP_REPO_BASE = (
    MCP_REPO.parent if MCP_REPO.parent.name == _paths.DEFAULT_MCP_REPO_DIRNAME
    else HOME_ROOT / _paths.DEFAULT_MCP_REPO_DIRNAME
)

FAILURES = []

# git 可用性：有两处断言要靠 `git ls-files` 校验「产物/凭证未入库」。
# 无 git（精简环境、zip 解压安装）时降级为提示，而不是抛 FileNotFoundError 让自检整体变红。
_HAS_GIT = shutil.which("git") is not None

# 是否处于「插件/仓库布局」：技能被单独安装到某平台的技能目录时（例如 ~/.claude/skills/video2book/），
# 仓库级文件（README / pyproject / 平台清单）根本不存在——这类断言必须降级为提示，
# 否则用户装完技能一跑自检就是一片红，反而以为装坏了。
#
# 探测标记**必须避开 `CLAUDE.md`**：单独安装时 `REPO_ROOT` 就是宿主配置根（如 `~/.claude`），
# 那里几乎必然存在用户自己的全局 `CLAUDE.md`——拿它当标记会把「单独安装」误判成「仓库布局」，
# 于是所有仓库级断言集体去读不存在的文件而全部报红。改用本仓库特有的清单目录与 `pyproject.toml`。
PLUGIN_LAYOUT = any(
    (REPO_ROOT / rel).exists()
    for rel in (".codex-plugin", ".claude-plugin", ".agents", "pyproject.toml")
)


def _require_plugin_layout(name: str) -> bool:
    """仓库级断言的前置门：不在插件布局时打印说明并让调用方提前返回。"""
    if PLUGIN_LAYOUT:
        return True
    print(f"       (技能为单独安装，未检测到插件/仓库布局，跳过仓库级断言：{name})")
    return False


def check(name, fn):
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as err:
        FAILURES.append((name, err))
        print(f"[FAIL] {name}: {type(err).__name__}: {err}")


def check_imports():
    import src.cli  # noqa: F401
    import src.core.console  # noqa: F401
    import src.core.fsutil  # noqa: F401
    import src.core.paths  # noqa: F401
    import src.core.pipeline  # noqa: F401
    import src.core.workspace  # noqa: F401
    import src.core.ingestion  # noqa: F401
    import src.generator.topic_planner  # noqa: F401
    import src.generator.integrator  # noqa: F401
    import src.generator.block_synthesizer  # noqa: F401


def check_cli_help():
    # 一集一篇的 `transcribe` 入口已随旧链路整体移除，不许回加
    for sub in ("parse", "audio",
                "pipeline", "merge-audio", "split-transcript",
                "cluster-notes", "cluster-articles", "dedup",
                "cleanup", "sync", "login", "logout", "info"):
        res = run_quiet(
            [sys.executable, str(SKILL_ROOT / "src" / "cli.py"), sub, "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
        )
        assert res.returncode == 0, f"`{sub} --help` 退出码 {res.returncode}: {res.stderr[:200]}"

    # 块级转录是阶段一**唯一**的取音链路：块时长必须可覆盖（不写死）；「逐集听音」的老入口
    # 已经整体移除，不许回加——它的存在等于给「按分集标题编长文」留了一条后门。
    res = run_quiet(
        [sys.executable, str(SKILL_ROOT / "src" / "cli.py"), "pipeline", "--help"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    assert "--block-minutes" in res.stdout, "pipeline 缺少 --block-minutes（块时长必须可配置）"
    for 已移除 in ("--no-merge", "--chunk-minutes"):
        assert 已移除 not in res.stdout, f"pipeline 不该再有 {已移除}（逐集听音链路已整体移除）"


def check_repo_separation():
    """三域分离契约：skill/ 与承载两个 MCP 的 omni-media/ 各自独立成仓，产物根在两者之外。

    容器布局（存在 `.bvb-home` 或 `$BVB_HOME`）下这些是硬约束；**独立克隆 / zip 解压安装**
    时容器根本就不存在，相应断言降级为提示——否则一份正常的独立使用会在自检第一步就 FAIL。
    """
    container = _paths.is_container_layout()

    if (REPO_ROOT / ".git").is_dir():
        assert (REPO_ROOT / ".gitattributes").is_file(), "仓库根缺少 .gitattributes（行尾契约）"
    else:
        print("       (skill/ 不是 git 工作树——zip 下载安装，跳过仓库边界断言)")

    if container:
        # Codex uses an empty root .git as a workspace boundary; it must not
        # become a real version-control repository containing tracked files.
        root_git = HOME_ROOT / ".git"
        if root_git.is_dir():
            commits = run_quiet(
                ["git", "-C", str(HOME_ROOT), "rev-list", "--all", "--count"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            tracked = run_quiet(
                ["git", "-C", str(HOME_ROOT), "ls-files"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            assert commits.returncode == 0, f"无法识别容器根 .git 标记: {commits.stderr[:200]}"
            assert tracked.returncode == 0, f"无法识别容器根 .git 标记: {tracked.stderr[:200]}"
            assert (commits.stdout or "").strip() == "0", \
                "容器根 .git 已包含提交；它是 Codex 工作区标记，不应进行真实版本控制"
            assert not (tracked.stdout or "").strip(), \
                "容器根 .git 已跟踪文件；它是 Codex 工作区标记，不应进行真实版本控制"
    else:
        print(f"       (未检测到容器布局标记，跳过「容器根不得是 git 仓库」断言: home={HOME_ROOT})")

    # 容器布局下，产物根必须位于两个仓库工作树之外。独立 GitHub clone
    # 没有容器标记，默认产物本就落在工作目录下的 output/，此时由 .gitignore
    # 保障安全，不得把合法安装误判为错误。
    if PLUGIN_LAYOUT and container:
        for repo_name, repo_root in (("skill", REPO_ROOT),
                                     (_paths.DEFAULT_MCP_REPO_DIRNAME, MCP_REPO_BASE)):
            if not repo_root.exists():
                continue
            try:
                PRODUCTS_ROOT.relative_to(repo_root)
            except ValueError:
                continue
            raise AssertionError(f"产物根 {PRODUCTS_ROOT} 位于 {repo_name} 仓库工作树内")

    if container:
        if MCP_REPO_BASE.is_dir():
            assert (MCP_REPO_BASE / ".git").is_dir(), \
                f"{_paths.DEFAULT_MCP_REPO_DIRNAME}/ 应是独立 git 仓库（缺 .git）"
        elif (HOME_ROOT / _paths.DEFAULT_MCP_DIRNAME).is_dir():
            # 迁移前的旧布局：mcp/ 曾自己就是一个仓库
            assert (HOME_ROOT / _paths.DEFAULT_MCP_DIRNAME / ".git").is_dir(), \
                "mcp/ 应是独立 git 仓库（缺 .git）"
        assert not (REPO_ROOT / "omni-media-mcp").exists(), "仓库根不应再残留 omni-media-mcp/"

    # 产物根必须可用：不存在就按工具的默认语义建出来（任何命令首次写入也会建它），
    # 这样全新克隆下自检不必依赖「恰好已经跑过一次 pipeline」。
    if not PRODUCTS_ROOT.is_dir():
        try:
            PRODUCTS_ROOT.mkdir(parents=True, exist_ok=True)
            print(f"[*] 产物根此前不存在，已按默认语义自动创建: {PRODUCTS_ROOT}")
        except OSError as err:
            raise AssertionError(f"产物根不存在且无法创建: {PRODUCTS_ROOT}（{err}）")


def check_products_root_resolution():
    """产物根解析契约（**容器根可选，默认落在工作目录**）：

    * 有容器标记**且当前工作目录在该容器内** → `<home>/output`（容器布局的历史锚点不变）；
    * 其余情况（无标记，或从容器外调用——例如技能软链进平台技能目录后）→ `<cwd>/output`；
    * `$BVB_OUTPUT_DIR` 是**受支持的显式覆盖**（SKILL.md / README 都写明），此时只要求解析自洽。

    三条分支都用纯函数 `products_root_for(home, cwd)` 直接断言，不依赖本机恰好处于哪种布局。
    """
    desc = _paths.describe()
    pinned = desc["container_pinned"]
    inside = desc["cwd_inside_container"]
    from_env = desc["products_from_env"]

    # ① 三分支的纯函数语义（任何环境下都必须成立）
    import tempfile as _tf

    with _tf.TemporaryDirectory() as outside:
        外部 = Path(outside).resolve()
        assert _paths.products_root_for(None, 外部) == 外部 / _paths.DEFAULT_PRODUCTS_DIRNAME, \
            "无容器根时应取「cwd/output」"
        assert _paths.products_root_for(HOME_ROOT, 外部) == 外部 / _paths.DEFAULT_PRODUCTS_DIRNAME, \
            "cwd 在容器之外时应取「cwd/output」（技能软链到平台目录后的默认行为）"
        assert _paths.products_root_for(HOME_ROOT, HOME_ROOT) == HOME_ROOT / _paths.DEFAULT_PRODUCTS_DIRNAME, \
            "cwd 就是容器根时应取「<home>/output」"
        assert _paths.products_root_for(HOME_ROOT, HOME_ROOT / "output") == HOME_ROOT / _paths.DEFAULT_PRODUCTS_DIRNAME, \
            "cwd 在容器内子目录时应取「<home>/output」"

    if from_env:
        print(f"       (产物根由 ${_paths.ENV_OUTPUT_DIR} 覆盖，跳过「等价于 <home>/output」断言)")
    elif pinned and inside:
        expected = HOME_ROOT / "output"
        assert PRODUCTS_ROOT == expected, f"产物根解析异常: {PRODUCTS_ROOT} != {expected}"
    else:
        assert PRODUCTS_ROOT == _paths.products_root_for(None), \
            f"容器布局未生效时产物根应跟随工作目录: {PRODUCTS_ROOT}"
    assert _paths.resolve_base_dir(None) == PRODUCTS_ROOT, "空 --base-dir 未解析到产物根"
    assert _paths.resolve_base_dir("") == PRODUCTS_ROOT, "空字符串 --base-dir 未解析到产物根"
    assert _paths.default_base_dir() == str(PRODUCTS_ROOT), "default_base_dir 与产物根不一致"
    # manifest 相对路径基准 = 容器根，因此历史 `output/<task>/...` 字面值继续有效。
    # 用**真实的换算函数**验证：自己拼一个 HOME_ROOT/output 前缀、再断言该路径以它开头，是恒真式
    # （原先那句在任何布局下都不可能失败，等于没检查）。
    # 产物根被 $BVB_OUTPUT_DIR 覆盖、或落在容器根之外时该换算本就无意义，故只在容器布局生效时验证。
    if pinned and inside and not from_env:
        from src.core.workspace import TaskWorkspace

        rel = TaskWorkspace.to_relative(PRODUCTS_ROOT / "__probe__" / "模块01_甲_精读全书.md")
        assert rel.startswith("output/"), \
            f"manifest 相对路径基准不是容器根下的 output/（实际 {rel!r}）"

    # 跨工作目录解析（真跑子进程，验证 cwd 确实参与判定）：
    # * 容器外的目录 → 解析到**该目录自己的** output/（默认语义）；
    # * 容器内的任意工作目录 → 解析到 <home>/output；
    # * $BVB_OUTPUT_DIR 覆盖时 → 到哪都是同一个值。
    import tempfile

    probe = (
        "import sys; sys.path.insert(0, r'%s');"
        "from src.core import paths; print(paths.products_root())" % SKILL_ROOT
    )

    def _products_from(cwd: str) -> str:
        res = run_quiet(
            [sys.executable, "-c", probe],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        return (res.stdout or "").strip().splitlines()[-1] if res.stdout else ""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp).resolve()
        got = _products_from(str(tmp_root))
        if from_env:
            assert got == str(PRODUCTS_ROOT), \
                f"$BVB_OUTPUT_DIR 覆盖时到哪都应一致: {got!r} != {PRODUCTS_ROOT}"
        else:
            expected = str(tmp_root / _paths.DEFAULT_PRODUCTS_DIRNAME)
            assert got == expected, \
                f"容器之外的工作目录应解析到它自己的 output/: {got!r} != {expected!r}"

    if pinned and not from_env and HOME_ROOT.is_dir():
        got2 = _products_from(str(HOME_ROOT))
        expected2 = str(HOME_ROOT.resolve() / _paths.DEFAULT_PRODUCTS_DIRNAME)
        assert got2 == expected2, f"容器内任意工作目录都应解析到 <home>/output: {got2!r} != {expected2!r}"


def check_no_cross_repo_imports():
    """互不打扰：技能侧代码不得 import omni_media_mcp；MCP 侧不得 import src。"""
    import ast as _ast
    import re as _re

    def _imported_modules(path: Path) -> set:
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                if node.module and node.level == 0:
                    names.add(node.module.split(".")[0])
        return names

    # 技能侧：允许在「注释/字符串」里提到 MCP，但不允许真的 import
    offenders = []
    for path in list((SKILL_ROOT / "src").rglob("*.py")) + list((SKILL_ROOT / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "selfcheck.py":
            continue  # selfcheck 只以子进程方式调用 MCP 自检，不 import
        if "omni_media_mcp" in _imported_modules(path):
            offenders.append(path.relative_to(SKILL_ROOT).as_posix())
    assert not offenders, f"技能侧不得 import MCP 包: {offenders}"

    # 技能侧源码不得出现 omni_media_mcp 的 import 文本（防止动态 import 绕过）
    for path in (SKILL_ROOT / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not _re.search(r"^\s*(import|from)\s+omni_media_mcp", text, _re.M), \
            f"{path.relative_to(SKILL_ROOT)} 出现了对 MCP 包的 import"

    # 反向扫描两个 MCP：① 都不得 import 技能包 `src`；② **运行代码不得互相 import**。
    # ② 是收进同一个仓库后**新增**的隔离义务——此前两者分属不同仓库、天然隔离，
    # 现在同仓，必须由断言守住（对外承诺见 omni-media/README.md「两个服务互不 import」）。
    # 扫描整个 MCP 仓库目录（含 tests/ 与各自的 selfcheck.py）：conftest 之类同样会插 sys.path。
    #
    # 唯一豁免：`mcp-ext/tests/test_compatibility.py` **故意**同时 import 两侧——那正是它验证
    # 「线上契约同构」的手段（带 `requires_native` skipif 守卫，对侧缺席即整组跳过）。
    # 豁免只针对「互不 import」这一条；`src` 那条对它照查。
    cross_exempt = "tests/test_compatibility.py"
    for mcp_root, foreign in ((MCP_REPO, "omni_media_ext"), (MCP_EXT_REPO, "omni_media_mcp")):
        if not mcp_root.is_dir():
            continue
        bad = []
        for path in mcp_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(mcp_root).as_posix()
            mods = _imported_modules(path)
            # AST 只看 import 语句；对 `src` 再补一层文本扫描，防动态导入绕过
            if "src" in mods or _re.search(r"^\s*(?:import|from)\s+src\b", text, _re.M) \
                    or (foreign in mods and rel != cross_exempt):
                bad.append(rel)
        assert not bad, f"MCP 侧不得 import 技能包 src，运行代码也不得互引（契约测试除外）: {bad}"


def check_copied_skill_is_self_contained():
    """A copied skill directory must run from an unrelated working directory."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        copied = root / "video2book"
        shutil.copytree(
            SKILL_ROOT,
            copied,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        work = root / "work"
        work.mkdir()
        result = run_quiet(
            [sys.executable, str(copied / "src" / "cli.py"), "info"],
            cwd=str(work),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (result.stdout or "")[-500:]
        assert "Python 运行环境" in (result.stdout or "")
        assert str(work / "output") in (result.stdout or "")
    return "复制后的技能目录可从任意 cwd 独立运行"


def check_contract_parser():
    """Accept contract v1 and legacy v0; reject incompatible major versions."""
    from src.core.contract import (
        ContractCompatibilityError,
        parse_compatible_status,
    )

    assert parse_compatible_status("普通正文，无状态注释") is None
    legacy = '<!-- OMNI_STATUS: {"is_finished": true} -->'
    assert parse_compatible_status(legacy)["is_finished"] is True
    current = '<!-- OMNI_STATUS: {"contract_version": 1, "is_finished": false} -->'
    assert parse_compatible_status(current)["contract_version"] == 1
    incompatible = '<!-- OMNI_STATUS: {"contract_version": 2, "is_finished": true} -->'
    try:
        parse_compatible_status(incompatible)
    except ContractCompatibilityError as exc:
        assert "主版本 1" in str(exc)
    else:
        raise AssertionError("contract_version=2 必须被拒绝")
    return "契约 v1 可用，缺失按 legacy v0，其他主版本明确拒绝"


def check_note_planner_contract():
    """笔记归并契约：模块层**没有独立规划**（块即模块），归并校验按块号判定。

    覆盖基准是**实际集号**而不是序号：用户 `--range 9-87` 时工作区就是 P09–P87，
    工具无权要求它重排成 1..79（旧实现如此，导致这类工作区的阶段二永久不可达）。
    """
    import tempfile

    from src.generator.topic_planner import SemanticTopicPlanner as P

    # 第一趟「模块规划」整体废除：块边界来自音频装箱（audio/_blocks/blocks.json），
    # 再让 Agent 划一遍模块就是把同一件事做两遍，两套边界必然打架。
    for 已移除 in ("PLAN_PROMPT", "export_plan_task", "build_planning_prompt", "validate_plan",
                  "resolve_blocks", "salvage_blocks", "placeholder_blocks", "load_cached_plan",
                  "plan", "enforce_size_cap", "episode_corpus_bytes", "collect_article_titles",
                  "PLACEHOLDER_BLOCK_SIZE", "STATUS_PENDING", "fallback_heuristic_plan",
                  "find_gaps"):
        assert not hasattr(P, 已移除), f"模块层不再有独立规划，不得回加：{已移除}"

    # 集号基准 = 实际集号集合（不是序号 1..N）
    pages = list(range(9, 88))
    assert P.describe_pages(pages) == "P09–P87", P.describe_pages(pages)
    assert P.describe_episodes([9]) == "P09" and P.describe_episodes([9, 10]) == "P09–P10"
    assert P.describe_blocks([3, 1]) == "块 01、块 03", P.describe_blocks([3, 1])

    # 归并校验：每个块恰好被认领一次，推导出的集号恰好全覆盖
    blocks = [
        {"block_id": 1, "title": "A", "episodes": pages[:40]},
        {"block_id": 2, "title": "B", "episodes": pages[40:]},
    ]
    ok, _ = P.validate_note_plan(
        [{"note_id": 1, "note_title": "跨块笔记", "blocks": [1, 2]}], blocks, pages)
    assert ok, "全覆盖且每块认领一次应通过"
    assert P.note_episodes({"blocks": [1, 2]}, blocks) == pages, "跨块笔记的集号未推导齐全"
    for bad, why in (
        ([{"note_id": 1, "note_title": "x", "blocks": [1]}], "漏掉块 2"),
        ([{"note_id": 1, "note_title": "x", "blocks": [1, 2, 2]}], "块 2 被认领两次"),
        ([{"note_id": 1, "note_title": "x", "blocks": [1, 3]}], "引用了不存在的块"),
        ([{"note_id": 1, "note_title": "x", "blocks": []}], "未认领任何块"),
    ):
        assert not P.validate_note_plan(bad, blocks, pages)[0], f"应被拒绝：{why}"

    # 长文标题读取：H1 优先，文件名次之（模块XX_ 前缀与 _精读长文 后缀都要剥掉）
    with tempfile.TemporaryDirectory() as tmp:
        art = Path(tmp) / "模块03_核心语法与函数_精读长文.md"
        art.write_text("# 变量、作用域与函数" + chr(10) + chr(10) + "正文…" + chr(10), encoding="utf-8")
        assert P.read_article_title(art) == "变量、作用域与函数", P.read_article_title(art)
        art.write_text("没有 H1 的长文" + chr(10), encoding="utf-8")
        assert P.read_article_title(art) == "核心语法与函数", P.read_article_title(art)


def check_docs_no_dangling_section_refs():
    """文档不得引用**不存在的小节号**。

    这条守的是一个真实踩过的坑：删掉 §7.4 之后，第 7 节的表格里仍写着「按 § 7.4 的
    笔记规范产出」——读者与 Agent 都会去找一个不存在的小节。
    """
    import re as _re

    for rel in ("SKILL.md", "references/delivery_matrix.md"):
        text = (SKILL_ROOT / rel).read_text(encoding="utf-8")
        # 该文件里真实存在的小节号（## / ### 标题里的编号）
        existing = set(_re.findall(r"^#{2,3}\s+(\d+(?:\.\d+)*)\.?", text, _re.MULTILINE))
        top = {num.split(".")[0] for num in existing}
        for raw in _re.findall(r"§\s*(\d+(?:\.\d+)*)", text):
            # `§ 5.3` 这类跨层引用：只要顶层小节存在即认（避免把「见 § 5 表格」判错）
            head = raw.split(".")[0]
            if raw in existing or head in top:
                continue
            raise AssertionError(f"{rel} 引用了不存在的小节号：§ {raw}")


def check_integrator_no_hardcoded_course():
    """通用整编器不得再内嵌任何具体课程数据。"""
    import inspect

    from src.generator.integrator import ArticleIntegrator

    text = (SKILL_ROOT / "src" / "generator" / "integrator.py").read_text(encoding="utf-8")
    for needle in ("微机原理", "8253", "8255A", "黑马程序员", "核心语法-", "函数基础"):
        assert needle not in text, f"integrator.py 仍含硬编码课程数据: {needle}"

    sig = inspect.signature(ArticleIntegrator.run)
    assert sig.parameters["course_title"].default is inspect.Parameter.empty, \
        "run() 的 course_title 应为必传参数"


def check_transcript_pipeline():
    """块级链路：模块长文任务书入口在、块级转录入口在，**逐集**的两个入口不得回流。

    注意这条断言的性质变了：逐字稿本身不再是禁忌——块级转录流水线**要求**它落盘到
    `subtitles/`（它是转录与写作两类角色之间的接口）。守的是「按集转录」这条老路径：
    它一门 200 集的课就要调 200 次取音接口，正是本次要消掉的成本。
    """
    from src.core import pipeline

    assert hasattr(pipeline, "export_block_article_task"), "模块长文任务书入口应已提供"
    assert hasattr(pipeline, "export_block_transcribe_task"), "块级转录任务书入口应已提供"
    assert not hasattr(pipeline, "export_article_task"), \
        "逐集长文入口（export_article_task）不得回加：成文必须按块派发"
    assert not hasattr(pipeline, "export_transcribe_task"), \
        "逐集转录任务书入口（export_transcribe_task）不得回加：转录必须按块派发"
    assert str(pipeline.TRANSCRIBE_TIMESTAMP_INSTRUCTION).strip(), \
        "转录时间戳要求不得为空——缺了它，块级逐字稿无法机械切回分集"


def check_audio_block_contract():
    """块级转录的机器契约：块时长区间可配不写死、超长集按上限劈分、装箱不劈散、切分确定、复用优先、幂等。

    这些是本次改造的核心不变量，全部是可复算的纯逻辑断言（只有拼接那一段真跑 ffmpeg，
    用 ffmpeg 合成的正弦音，不依赖任何课程音频）。
    """
    import tempfile

    from src.core.audio_merger import (
        DEFAULT_BLOCK_MAX_MINUTES,
        DEFAULT_BLOCK_MIN_MINUTES,
        DEFAULT_BLOCK_MINUTES,
        DEFAULT_ONESHOT_LIMIT_MINUTES,
        ENV_BLOCK_MINUTES,
        ENV_ONESHOT_LIMIT_MINUTES,
        AudioMerger,
    )
    from src.core.transcript_splitter import TranscriptSplitter
    from src.core.workspace import TaskWorkspace

    # 1) 块时长默认 50（区间中段）但**不得写死**：环境变量可覆盖，且目标超过上限时被夹紧
    assert DEFAULT_BLOCK_MINUTES == 50.0, f"块时长目标默认值应为 50 分钟，实际 {DEFAULT_BLOCK_MINUTES}"
    assert DEFAULT_ONESHOT_LIMIT_MINUTES == 75.0, "单块硬上限默认应为 75 分钟（取音侧整片就绪阈值）"
    assert (DEFAULT_BLOCK_MIN_MINUTES, DEFAULT_BLOCK_MAX_MINUTES) == (40.0, 60.0), "块时长区间默认应为 40–60 分钟"
    assert AudioMerger.block_minutes() == 50.0, "未设环境变量时应取默认 50"
    _limits = AudioMerger.limits()
    assert (_limits["target"], _limits["ceiling"], _limits["effective"]) == (50.0, 75.0, 50.0), _limits
    assert (_limits["min"], _limits["max"]) == (40.0, 60.0), _limits

    _old_target = os.environ.get(ENV_BLOCK_MINUTES)
    _old_ceiling = os.environ.get(ENV_ONESHOT_LIMIT_MINUTES)
    try:
        os.environ[ENV_BLOCK_MINUTES] = "45"
        assert AudioMerger.block_minutes() == 45.0, "块时长目标未跟随环境变量（即写死了）"
        assert AudioMerger.limits()["effective"] == 45.0
        os.environ[ENV_ONESHOT_LIMIT_MINUTES] = "30"
        assert AudioMerger.limits()["effective"] == 30.0, "目标超过硬上限时应被夹紧到上限"
        os.environ[ENV_BLOCK_MINUTES] = "0"
        assert AudioMerger.block_minutes() == 50.0, "非正数环境变量应回退默认"
        os.environ[ENV_BLOCK_MINUTES] = "abc"
        assert AudioMerger.block_minutes() == 50.0, "非法环境变量应回退默认（不因配置笔误中断）"
    finally:
        for _key, _val in ((ENV_BLOCK_MINUTES, _old_target), (ENV_ONESHOT_LIMIT_MINUTES, _old_ceiling)):
            if _val is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _val

    # 2) 装箱：不重不漏、块内连续、绝大多数块落进 40–60 区间、硬上限不越
    durations = [600.0] * 7 + [1800.0] + [300.0] * 4
    groups = AudioMerger.pack(durations, 50.0, 60.0, 40.0)
    flat = sorted(i for g in groups for i in g)
    assert flat == list(range(len(durations))), f"装箱后集号有重有漏: {groups}"
    in_band = 0
    for group in groups:
        assert group == list(range(group[0], group[0] + len(group))), f"块内必须连续: {group}"
        span = sum(durations[i] for i in group)
        assert span <= 75.0 * 60 + 1e-6, f"块越过硬上限: {group} = {span}s"
        if 40.0 * 60 - 1 <= span <= 60.0 * 60 + 1:
            in_band += 1
    assert in_band >= len(groups) - 1, f"绝大多数块应落进 40–60 区间: {groups}"
    # 超长单集：绝不能与别集同块
    over = AudioMerger.pack([5000.0, 600.0], 50.0, 60.0, 40.0)
    assert any(len(g) == 1 and 0 in g for g in over), f"超长单集未被单独成块: {over}"

    # 2b) 装箱单元：超长集劈上下两半；劈完任一段不合格就不劈
    def _records(durations_sec):
        return [
            {"page": i + 1, "duration": float(d), "path": f"P{i + 1:02d}.m4a",
             "title": f"第{i + 1}集", "size": 1, "codec": "aac", "sample_rate": 16000, "channels": 1}
            for i, d in enumerate(durations_sec)
        ]

    units = AudioMerger.plan_units(_records([600.0] * 5 + [6136.0]), 40.0, 60.0)   # 5×10 分钟 + 102 分钟
    labels = [u["label"] for u in units]
    assert "P06上" in labels and "P06下" in labels, f"102 分钟单集未劈成上下两半: {labels}"
    for unit in units:
        if unit["split"]:
            assert 36.0 * 60 - 1 <= unit["duration_sec"] <= 60.0 * 60 + 1, unit
    only61 = AudioMerger.plan_units(_records([61 * 60.0]), 40.0, 60.0)
    assert len(only61) == 1 and not only61[0]["split"], "61 分钟劈两半会双低于下限，应当不劈"
    u76 = AudioMerger.plan_units(_records([76 * 60.0]), 40.0, 60.0)
    assert len(u76) == 2 and all(u["split"] for u in u76), f"76 分钟应劈成两段（各 ≈38 分钟）: {u76}"
    whole = AudioMerger.plan_units(_records([30 * 60.0, 25 * 60.0]), 40.0, 60.0)
    assert len(whole) == 2 and not any(u["split"] for u in whole), "未超上限的集不该被劈"

    # 2c) 块音频命名：课程_序号_语义标题(覆盖范围)；缺标题退化为首集标题，绝不空名
    assert AudioMerger.block_filename("3小时前端入门教程", 1, "HTML入门与常用标签", "P01-P05") \
        == "3小时前端入门教程_01_HTML入门与常用标签(P01-P05).m4a"
    assert AudioMerger.block_filename("课程", 2, "", "P06-P09") == "课程_02(P06-P09).m4a"
    # 覆盖范围含劈分腿时按「Pxx上/下」表达
    assert AudioMerger.block_span({"units": [{"label": "P12上"}, {"label": "P13下"}]}) == "P12上-P13下"
    assert AudioMerger.block_span({"episodes": [1, 6]}) == "P01-P06"

    # 3) 切分契约：两段式/三段式时间戳都认；无时间戳必须降级，绝不臆测归属
    assert TranscriptSplitter.parse_stamp("05:20") == 320.0, "两段式 MM:SS 未识别"
    assert TranscriptSplitter.parse_stamp("00:05:20") == 320.0, "三段式 HH:MM:SS 未识别"
    assert TranscriptSplitter.parse_stamp("320") == 320.0, "纯秒数未识别"
    segments = [
        {"page": 1, "start_sec": 0.0, "end_sec": 600.0, "start": "00:00:00", "end": "00:10:00"},
        {"page": 2, "start_sec": 600.0, "end_sec": 1200.0, "start": "00:10:00", "end": "00:20:00"},
    ]
    text = "[00:00:00] 第一集开场\n[00:05:00] 第一集中段\n[00:10:00] 第二集开场\n[00:15:00] 第二集中段\n"
    outcome = TranscriptSplitter.split(text, segments)
    assert outcome["mode"] == "timestamp", outcome["mode"]
    assert outcome["buckets"][1] == ["第一集开场", "第一集中段"], outcome["buckets"][1]
    assert outcome["buckets"][2] == ["第二集开场", "第二集中段"], outcome["buckets"][2]
    assert outcome["stats"]["anchored_boundaries"] == 1, outcome["stats"]
    assert TranscriptSplitter.split("没有任何时间戳的正文", segments)["mode"] == "unsplit", \
        "无时间戳时必须降级为 unsplit，不得按位置硬切"
    thin = "[00:00:00] 开场\n[00:15:00] 交界附近没有时间戳\n"
    assert TranscriptSplitter.split(thin, segments)["stats"]["anchored_boundaries"] == 0, \
        "交界处缺时间戳时未如实报告（会让错位切分蒙混过关）"

    # 3b) 稀疏时间戳过度归属必须从“提示”升级为“整块隔离”：
    #     只要边界不可信，就不能把块内任何一集当作 ready 送进写作派发。
    sparse_segments = [
        {"page": 18, "start_sec": 0.0, "end_sec": 600.0, "start": "00:00:00", "end": "00:10:00", "duration_sec": 600.0},
        {"page": 19, "start_sec": 600.0, "end_sec": 1200.0, "start": "00:10:00", "end": "00:20:00", "duration_sec": 600.0},
        {"page": 20, "start_sec": 1200.0, "end_sec": 1800.0, "start": "00:20:00", "end": "00:30:00", "duration_sec": 600.0},
    ]
    sparse_text = (
        "[00:00:00] P18 开场\n"
        + "\n".join(f"这是实际属于 P19 的无时间戳正文第 {i} 行" for i in range(1, 30))
        + "\n[00:20:00] P20 开场\n"
    )
    sparse_out = TranscriptSplitter.split(sparse_text, sparse_segments)
    assert [item["page"] for item in sparse_out["stats"]["over_assigned"]] == [18], sparse_out["stats"]
    assert any("分到" in line and "P18" in line for line in sparse_out["diag"]), sparse_out["diag"]

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="suspect_split", base_dir=tmp)
        block = {"block_id": 3, "episodes": [18, 19, 20], "segments": sparse_segments}
        stale_path = TranscriptSplitter.episode_path(ws, 18, "十八")
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text("上一轮的污染稿", encoding="utf-8")

        suspect = TranscriptSplitter.write_episode_transcripts(
            ws,
            block,
            sparse_text,
            titles={18: "十八", 19: "十九", 20: "二十"},
        )
        assert suspect["status"] == "suspect", suspect
        assert suspect["files"] == {}, "边界可疑时不得放行任何分集稿"
        assert suspect["suspect_pages"] == [18, 19, 20], suspect
        assert stale_path.exists(), "已有文件应保留供排障"
        assert TranscriptSplitter.existing_episode_transcript(ws, 18, "十八") is None, \
            "带 suspect 标记的旧文件不得被派发"

        AudioMerger.save_manifest(ws, {
            "version": AudioMerger.MANIFEST_VERSION,
            "blocks": [block],
            "target_minutes": 30.0,
            "effective_limit_minutes": 75.0,
        })
        cli_result = run_quiet(
            [sys.executable, str(SKILL_ROOT / "src" / "cli.py"), "split-transcript", str(ws.root_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        assert cli_result.returncode == 3, \
            f"suspect 块未以非零退出码阻断派发: {cli_result.returncode}\n{cli_result.stdout}"

        valid_text = (
            "[00:00:00] P18 正确内容\n"
            "[00:10:00] P19 正确内容\n"
            "[00:20:00] P20 正确内容\n"
        )
        fixed = TranscriptSplitter.write_episode_transcripts(
            ws,
            block,
            valid_text,
            titles={18: "十八", 19: "十九", 20: "二十"},
        )
        assert fixed["status"] == "split", fixed
        assert sorted(fixed["files"]) == [18, 19, 20], fixed
        assert TranscriptSplitter.existing_episode_transcript(ws, 18, "十八") == stale_path, fixed
        assert "上一轮的污染稿" not in stale_path.read_text(encoding="utf-8"), \
            "suspect 解除后必须用可靠切分覆盖旧污染稿"
        assert not TranscriptSplitter.suspect_path(ws, 18, "十八").exists(), \
            "可靠重切后 suspect 标记未解除"

    # 4) 命名与复用优先级 + 契约登记
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="block_contract", base_dir=tmp)
        assert TranscriptSplitter.episode_path(ws, 3, "绪论").name == "P03_绪论_逐字稿.md"
        assert TranscriptSplitter.block_path(ws, {"audio": "audio/_blocks/BLK02_P13-P17.m4a"}).name \
            == "BLK02_P13-P17_逐字稿.md"
        assert TranscriptSplitter.existing_episode_transcript(ws, 3, "绪论") is None
        # `_clean.txt`（旧链路的"人工清洗稿"）曾是伪逐字稿混进流水线的放行口：已彻底移除，
        # 任何来路不明的文本顶着这个名字都不再被当成语料。
        assert not hasattr(TranscriptSplitter, "legacy_path"), \
            "已删除的 _clean.txt 兼容入口不许回加（它是 2026-09 伪逐字稿事故的入口）"
        dirty = ws.subtitles_dir / "P03_绪论_clean.txt"
        dirty.write_text("来路不明的一段文本", encoding="utf-8")
        assert TranscriptSplitter.existing_episode_transcript(ws, 3, "绪论") is None, \
            "`_clean.txt` 不得再被当成逐字稿复用"
        fresh = TranscriptSplitter.episode_path(ws, 3, "绪论")
        fresh.write_text("块级转录产物", encoding="utf-8")
        assert TranscriptSplitter.existing_episode_transcript(ws, 3, "绪论") == fresh, \
            "已切出的分集逐字稿应被认作可用语料"

        # 4b) 块级稿的命名只取决于**块结构**：无收益装箱（每块仅一集、块音频直接指向该集原音频）
        #     时也不能与分集稿同名，否则两者互相覆盖
        noop_block = {"block_id": 1, "episodes": [8], "audio": "audio/P08_测试集.m4a"}
        block_named = TranscriptSplitter.block_path(ws, noop_block).name
        assert block_named == "BLK01_P08_逐字稿.md", block_named
        assert block_named != TranscriptSplitter.episode_path(ws, 8, "测试集").name, \
            "块级稿与分集稿同名会互相覆盖"

        # 4c) 陈旧派发物清理：改块时长会改块编号与集号区间，旧任务书必须作废——
        #     否则它是一份「可被派发的幽灵任务」，照着它转录会去读一个不存在的块
        subtitles = Path(ws.subtitles_dir)
        (subtitles / "BLK01_P08_转录任务书.md").write_text("当前", encoding="utf-8")
        (subtitles / "BLK09_P70-P79_转录任务书.md").write_text("陈旧", encoding="utf-8")
        (subtitles / "BLK09_P70-P79_逐字稿.md").write_text("陈旧数据", encoding="utf-8")
        stale = AudioMerger.prune_orphans(ws, [noop_block])
        assert stale["removed_tasks"] == ["BLK09_P70-P79_转录任务书.md"], stale
        assert not (subtitles / "BLK09_P70-P79_转录任务书.md").exists(), "陈旧任务书未被作废"
        assert (subtitles / "BLK01_P08_转录任务书.md").exists(), "当前任务书被误删"
        assert stale["orphan_transcripts"] == ["BLK09_P70-P79_逐字稿.md"], stale
        assert (subtitles / "BLK09_P70-P79_逐字稿.md").exists(), \
            "块级逐字稿是数据产物，只应报告、不应删除（删了会连累已切出的分集稿）"

        assert "blocks_manifest" in TaskWorkspace.PATH_KEYS, \
            "块清单路径未登记进 PATH_KEYS，机器绝对路径会漏进 manifest"
        # 只有逐字稿、还没写长文的工作区不能被当成空壳（会被重建目录、丢掉既有语料）
        only_transcript = Path(tmp) / "只有逐字稿"
        (only_transcript / "subtitles").mkdir(parents=True, exist_ok=True)
        (only_transcript / "subtitles" / "P01_绪论_逐字稿.md").write_text("语料", encoding="utf-8")
        assert TaskWorkspace._populated(only_transcript) is True, \
            "只有逐字稿的工作区被判成空壳"
        # 渲染门禁必须把逐字稿与转录任务书排除在交付物之外（ASR 文本里围栏不闭合属正常）
        import render_compat_check as _rcc
        for suffix in ("_逐字稿.md", "_转录任务书.md"):
            assert suffix in _rcc.EXCLUDE_NAME_SUFFIXES, f"渲染门禁未排除 {suffix}"

        # 5) 真跑一次拼接 + 幂等（用 ffmpeg 合成正弦音，不依赖课程音频）
        if not shutil.which("ffmpeg"):
            print("    [skip] 未装 ffmpeg，跳过块拼接与幂等的真跑验证")
            return
        source_dir = Path(tmp) / "src_audio"
        source_dir.mkdir(parents=True, exist_ok=True)
        for index in (11, 12):
            target = source_dir / f"P{index}_测试集{index}.m4a"
            res = run_quiet(
                [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-acodec", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k", str(target)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120,
            )
            assert res.returncode == 0 and target.exists(), f"合成测试音频失败: {res.stderr[:200]}"
        ws2 = TaskWorkspace(task_name="block_merge", base_dir=tmp)
        for index in (11, 12):
            src_file = source_dir / f"P{index}_测试集{index}.m4a"
            (ws2.audio_dir / src_file.name).write_bytes(src_file.read_bytes())
        ws2.save_parts([{"page": 11, "title": "测试集11"}, {"page": 12, "title": "测试集12"}])

        first = AudioMerger.merge(ws2, [11, 12], target_minutes=1)
        assert first["status"] in ("merged", "noop"), f"首次合并状态异常: {first['status']}"
        assert len(first["blocks"]) == 1, f"两集两秒音频应合成一块: {first['blocks']}"
        block = first["blocks"][0]
        assert block["episodes"] == [11, 12] and len(block["segments"]) == 2
        assert abs(sum(s["duration_sec"] for s in block["segments"]) - block["duration_sec"]) < 1.5, \
            f"段表时长与块时长不一致: {block}"
        manifest_file = AudioMerger.manifest_path(ws2)
        assert manifest_file.exists(), "块清单未落盘"
        before = manifest_file.read_bytes()

        second = AudioMerger.merge(ws2, [11, 12], target_minutes=1)
        assert second["status"] == "cached", f"重跑未命中缓存（不幂等）: {second['status']}"
        assert manifest_file.read_bytes() == before, "重跑改写了块清单（不幂等）"
        loaded = AudioMerger.load_manifest(ws2)
        assert loaded and loaded["blocks"][0]["episodes"] == [11, 12]


def check_subprocess_timeouts():
    """子进程契约：所有外部程序调用都必须 (1) 走 run_quiet（抑制 Windows 控制台窗口）
    且 (2) 带硬超时；源码里不得再出现裸 subprocess.run / Popen。

    背景：ffmpeg/ffprobe 每次调用都会新建进程，宿主后台托管 + 多子智能体并发时
    Windows 会为每个控制台程序新开窗口（成片闪黑窗，一门 84 集课程约 250 次）。
    窗口抑制集中在 src/core/proc.py，此处防止有人回退成裸调用。
    """
    import src.core.audio_chunker as ac
    import src.core.audio_merger as am
    import src.core.local_media as lm
    import src.core.proc as proc_mod
    import ast as _ast

    assert lm.PROBE_TIMEOUT_SEC > 0 and lm.TRANSCODE_TIMEOUT_SEC > 0
    assert ac.PROBE_TIMEOUT_SEC == lm.PROBE_TIMEOUT_SEC
    # 转码（拼接/转码）口径现由块级合并侧引用：audio_chunker 只留取时长与时间格式化
    assert am.TRANSCODE_TIMEOUT_SEC == lm.TRANSCODE_TIMEOUT_SEC
    assert not hasattr(ac, 'chunk_audio'), '单集音频切片（chunk_audio）已随逐集听音移除，不许回加'
    assert not hasattr(ac, 'SUPPORTED_VIDEO_EXTS'), 'audio_chunker 不该再持有视频扩展名表（已归 local_media）'
    assert hasattr(proc_mod, "run_quiet") and hasattr(proc_mod, "CREATE_NO_WINDOW")

    import os as _os

    if _os.name == "nt":
        # Windows 下必须真正带上窗口抑制标志
        kwargs = proc_mod.quiet_kwargs()
        assert kwargs.get("creationflags") == proc_mod.CREATE_NO_WINDOW and proc_mod.CREATE_NO_WINDOW, \
            "run_quiet 在 Windows 下未启用 CREATE_NO_WINDOW"

    bare = []
    timeoutless = []
    for path in list((SKILL_ROOT / "src").rglob("*.py")) + list((SKILL_ROOT / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "proc.py":
            continue  # proc.py 是唯一允许直接调用 subprocess.run 的地方
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(SKILL_ROOT).as_posix()
        tree = _ast.parse(text)
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            func = node.func
            attr = getattr(func, "attr", None)
            base = getattr(func, "value", None)
            base_name = getattr(base, "id", None)
            if attr in {"run", "Popen", "call", "check_output"} and base_name in {"subprocess", "_sp"}:
                bare.append(f"{rel}:{node.lineno}")
            if getattr(func, "id", None) == "run_quiet":
                if not any(kw.arg == "timeout" for kw in node.keywords):
                    timeoutless.append(f"{rel}:{node.lineno}")
    assert not bare, "存在未抑制控制台窗口的裸子进程调用（应改走 run_quiet）: " + ", ".join(bare)
    assert not timeoutless, "run_quiet 调用缺少 timeout=（硬超时是强制契约）: " + ", ".join(timeoutless)


def check_mcp_repo_optional():
    """可选段落：存在 MCP 仓库时调用它自己的自检（技能侧不依赖 MCP）。

    MCP 的全部不变量（工具契约、limits、适配器、废弃链路）由 MCP 自己的 `selfcheck.py` 负责，
    避免两处断言各自漂移；找不到 MCP 仓库即跳过，不视为失败。
    """
    import subprocess as _sp

    # 探查契约：`mcp_repo()` 现在必须**实际存在于候选父目录里**，而不是拿 home_root() 拼字符串
    # （平台安装下 home_root() 只是提示值，拼出来的路径往往根本不存在）。
    bases = _paths.mcp_candidate_bases()
    assert bases, "MCP 候选父目录为空"
    assert bases[0] == PRODUCTS_ROOT.parent, \
        f"候选首位应为产物根的父目录（默认布局下与 omni-media/ 平级）: {bases[0]}"
    _mcp_root = _paths.mcp_repo()
    if _mcp_root.is_dir() and not os.environ.get(_paths.ENV_MCP_DIR, "").strip():
        assert _mcp_root.parent.name == _paths.DEFAULT_MCP_REPO_DIRNAME, \
            f"mcp_repo() 命中的路径形态异常（应为 <base>/omni-media/mcp）: {_mcp_root}"

    # 两个 MCP 现在同属 omni-media 仓库，**各自的断言集都要跑**：此前只跑 mcp 一侧，
    # 等于 mcp-ext 那份 selfcheck（体量更大）从技能侧永不触发。
    ran = []
    for mcp_root in (MCP_REPO, MCP_EXT_REPO):
        entry = mcp_root / "selfcheck.py"
        if not entry.is_file():
            print(f"       (未发现 {entry}，跳过)")
            continue
        res = run_quiet(
            [sys.executable, str(entry)],
            cwd=str(mcp_root), stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, timeout=300,
        )
        tail = "\n".join((res.stdout or "").strip().splitlines()[-4:])
        assert res.returncode == 0, f"{entry} 未通过（exit {res.returncode}）:\n{tail}"
        ran.append(str(entry))
    if ran:
        print("       (已调用 " + "、".join(ran) + ")")


def check_dead_modules_removed():
    # 技能目录内（随 skill 移动）
    for rel in (
        "src/core/http_client.py",
        "src/core/kernel_extractor.py",  # 逐集知识元特性随逐集链路一并移除
        "src/generator/cleaner.py",
        "src/generator/classifier.py",
        "src/generator/doc_builder.py",
        "scripts/validate_skill.py",
    ):
        assert not (SKILL_ROOT / rel).exists(), f"{rel} 应已删除"

    if not _require_plugin_layout("仓库根的死代码清单"):
        return
    # 仓库根（迁移前的老位置，与"技能已自包含"互斥）
    for rel in (
        "SKILL.md",
        "src",
        "scripts",
        "references",
        ".agents/skills",
        "requirements.txt",  # 空壳：5 行纯注释、全仓零引用；依赖声明在 pyproject + 平台清单里
        "tests",
        "MCP_TOOL_AUDIT_REPORT.md",
        "config.example.json",
        # 三域分离后，MCP 的实现不再属于本仓库（其死代码断言由 MCP 自己的 selfcheck.py 负责）
        "omni-media-mcp",
    ):
        assert not (REPO_ROOT / rel).exists(), f"仓库根不应存在 {rel}"


def check_host_artifacts_ignored():
    """宿主/编辑器旁路目录与产物根都不得进入任一仓库。

    .workbuddy、.zcode 这类目录由编辑器在会话中自动写入（含对话记忆）——三域分离后它们位于
    容器根，不在任何仓库工作树内；产物根同理。这里验证两件事：①「不在工作树内」这一结构事实
    （对 skill/ 与 MCP 仓库都查一遍 `git ls-files`）；② `skill/.gitignore` 仍留有安全网条目
    （防止有人把产物目录搬回仓库内）。MCP 仓库自身的安全网由各自的 `.gitignore` 负责。
    """
    import subprocess as _sp

    if not _require_plugin_layout("旁路目录/产物不入库"):
        return

    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in (".workbuddy", ".aide", ".zcode", "output", ".sessdata.json", ".archive"):
        assert name in ignore, f"{name} 未被 skill/.gitignore 覆盖（安全网缺失）"

    # 把可能存在的 MCP 仓库一并纳入校验（新布局认 omni-media/，旧布局认 mcp/）
    repos = [REPO_ROOT] + [
        p for p in (MCP_REPO_BASE, HOME_ROOT / _paths.DEFAULT_MCP_DIRNAME)
        if (p / ".git").is_dir()
    ]

    if not _HAS_GIT:
        print("       (未找到 git 命令，跳过「旁路目录/产物未入库」的 git 校验)")
    else:
        for repo in repos:
            tracked = run_quiet(
                ["git", "ls-files", "--", ".workbuddy", ".aide", ".zcode", "output", ".sessdata.json", ".archive"],
                cwd=str(repo), stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, timeout=60,
            )
            assert not tracked.stdout.strip(), \
                f"{repo.name} 仓库纳入了宿主旁路目录/产物: {tracked.stdout.strip()}"

    # 「旁路目录位于容器根」只在**容器布局**下才是结构事实：独立克隆时容器根根本不存在。
    if not _paths.is_container_layout():
        print(f"       (未检测到容器布局标记，跳过「旁路目录位于容器根」断言: home={HOME_ROOT})")
        return

    for name in (".workbuddy", ".zcode", "output"):
        assert (HOME_ROOT / name).exists() or name == ".zcode", f"容器根缺少 {name}"
        for repo in repos:
            try:
                (HOME_ROOT / name).relative_to(repo)
            except ValueError:
                continue
            raise AssertionError(f"{name} 位于 {repo.name} 仓库工作树内，应移到容器根")


def check_workspace_name_derivation():
    """工作区名的推导与找回（历史工作区名被截断过，这几条规则踩过坑，必须守住）。

    覆盖的真实故障：
    1. 标题超长时 BV 号被一起截掉 → 后续命令再也找不到工作区（实测黑马 `…_BV1sHU`）；
    2. `sanitize_name` strip 结尾下划线 → 推导名与磁盘名差一个字符，命令在别处建空壳；
    3. 空壳目录被当成已有工作区 → 对着空目录报「尚无任何语料」；
    4. 多个同源候选时乱认 → 静默读写到别门课的产物上。
    """
    from src.core.workspace import TaskWorkspace

    import tempfile

    long_title = "黑马程序员AI大模型NLP自然语言处理保姆级教程，PyTorch实现Transformer完整代码解析+预训练模型，一套搞定文本分类_翻译_情感分析等实战项目_"
    bvid = "BV14mdfBDE4Q"

    # 1) BV 号必须完整保留（它是找回工作区的唯一标识）
    name = TaskWorkspace.new_task_name(long_title, bvid)
    assert name.endswith(f"_{bvid}"), f"超长标题把 BV 号截掉了: {name!r}"
    assert len(name) <= TaskWorkspace.TASK_NAME_MAX, f"工作区名超出长度上限: {len(name)}"

    # 2) 名字幂等：再清洗一次不得变化（否则 __init__ 会把磁盘名改短）
    assert TaskWorkspace.sanitize_name(name) == name, \
        f"推导名再清洗会变形: {TaskWorkspace.sanitize_name(name)!r} != {name!r}"

    # 3) 结尾下划线/空格：标题尾部与磁盘名同源时，清洗不得吃掉有效字符
    assert TaskWorkspace.new_task_name("标题 ", "") == "标题"
    assert TaskWorkspace.new_task_name("标题_", "") == "标题"

    # 4) 同源判定：标题部分互为前缀才算同一门课
    a_bv, b_bv = "BV1aaaaaaaaa", "BV1bbbbbbbbb"
    assert TaskWorkspace._same_course(f"甲课程导论_{a_bv}", "甲课程导论")
    assert TaskWorkspace._same_course(f"甲课程导论_{a_bv}", f"甲课程导论与实战_{b_bv}")
    assert not TaskWorkspace._same_course(f"甲课程导论_{a_bv}", f"乙课程导论_{b_bv}"), "不同课被认成同源"
    assert not TaskWorkspace._same_course(f"甲_{a_bv}", "甲课程导论"), "过短的标题不该同源"

    # 5) 空壳不认、有料才认；多个同源候选时放弃（不猜）
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        real = base / TaskWorkspace.new_task_name(long_title, bvid)
        (real / "articles").mkdir(parents=True)
        (real / "parts.json").write_text("[]", encoding="utf-8")
        shell = base / TaskWorkspace.new_task_name(long_title, "")
        for sub in ("articles", "notes", "subtitles", "audio"):
            (shell / sub).mkdir(parents=True, exist_ok=True)

        assert not TaskWorkspace._populated(shell), "空壳目录被当成有料"
        assert TaskWorkspace._populated(real), "有料目录没被认出来"
        picked = TaskWorkspace.create(title=long_title, bvid=bvid, base_dir=base)
        assert picked.root_dir.name == real.name, \
            f"有料目录在盘上却另建了工作区: {picked.root_dir.name!r}"

        # 目标不存在、但盘上恰有一个同源目录 → 复用（历史截断名场景）
        # 独立目录做，避免与上面那个同源目录凑成「有歧义」把用例弄失效。
        with tempfile.TemporaryDirectory() as tmp2:
            base2 = Path(tmp2)
            stem = long_title[:40]
            legacy = base2 / f"{stem}_翻译_情感分析等实战项目_"
            (legacy / "articles").mkdir(parents=True)
            (legacy / "parts.json").write_text("[]", encoding="utf-8")
            (base2 / f"别的课程_{b_bv}").mkdir()
            (base2 / f"别的课程_{b_bv}" / "parts.json").write_text("[]", encoding="utf-8")
            got = TaskWorkspace._find_existing_by_bvid(base2, stem)
            assert got == legacy.name, f"同源的历史截断名没被找回: {got!r}"

            # 再加一个同源候选 → 有歧义，必须放弃而不是乱挑
            (base2 / f"{stem}续篇_{b_bv}").mkdir()
            (base2 / f"{stem}续篇_{b_bv}" / "parts.json").write_text("[]", encoding="utf-8")
            assert TaskWorkspace._find_existing_by_bvid(base2, stem) is None, \
                "多个同源候选时应当放弃（不猜），却挑了一个"


# 宿主私有工具名黑名单：技能正文只讲**行动语义**，写死这些名字会让技能换个平台直接失效。
# 注意 `bash` 不在名单里——它是 Markdown 围栏语言标识（```bash），不是工具调用。
_PRIVATE_TOOL_NAMES = (
    "view_file",
    "write_to_file",
    "read_file",
    "apply_patch",
    "todowrite",
    "todo_write",
    "webfetch",
    "web_fetch",
    "str_replace_editor",
    "multi_edit",
    "create_file",
    "edit_file",
    "search_files",
    "run_command",
    "list_dir",
)

# 技能必须覆盖的宿主平台（与 references/install.md 的对照表同源）。
#
# **只收有实据的平台**：claude / codex / 通用 agents 的清单 schema 有真实原文，opencode 的工具名映射
# 已核实。没有实据的平台一律不进这个元组——一旦列进来，下面的断言就会逼着仓库留下"待确认"占位记录。
HOST_TARGETS = ("claude", "codex", "opencode")

# 未证实平台与外部参照项目名：**文档层不得出现**。
#
# 为什么禁止：没有实据的平台名一旦写进文档，使用者会以为技能支持它，装上却可能失效；
# 指向外部项目名则会把读者引向仓库之外（本仓库的策略是按 Agent Skills 规范自述）。
# 要支持新平台：**先取得实据**（官方文档 / 官方清单 schema / 实测工具列表），
# 再补 `references/host-tools/<平台>.md` 并加进 `HOST_TARGETS`，而不是先留一个"待确认"文件。
#
# 刻意豁免（不是漏网）：
#   - `.gitignore`：`.zcode/` `.workbuddy/` `.aide/` 是**防误提交的安全网**（宿主会自动往工作目录
#     写对话记忆），它们保护仓库，不是支持声明，因此不在扫描范围内；
#   - 本文件自身：这份禁用名单与容器根忽略规则断言的字面量就写在里面。
#
# ⚠️ 新增平台支持时是**两步**（漏一步自检就会红，且失败信息只会说"出现未证实平台名"）：
#   ① 补 `references/host-tools/<平台>.md` 并加进 `HOST_TARGETS`；
#   ② 把该名字从下面这个元组里**移除**。
#
# ⚠️ 「豆包」在名单里意味着：`references/` 下不得再引用课程原文里出现的该产品举例
#   （它可能作为讲师的举例出现在教材内容里）。这类原文若要进仓库，应先脱敏。
_UNVERIFIED_TRACE_NAMES = ("zcode", "workbuddy", "dsh", "doubao", "豆包",
                           "antigravity", "superpowers", "obra")


def check_skill_root_layout():
    """技能自包含布局：仓库根是**插件单元**，技能本体与它依赖的工具链同住 `skills/<name>/`。

    这是「只安装核心 skill 及依赖，不安装多余内容」的结构保证：
    安装 = 复制或软链 `skills/video2book/` 这**一个**目录。
    """
    name = SKILL_ROOT.name
    assert name == "video2book", f"技能目录名异常：{SKILL_ROOT}"
    assert SKILL_ROOT.parent.name == "skills", f"技能目录应位于 skills/ 下：{SKILL_ROOT}"

    for rel in ("SKILL.md", "references", "scripts", "src"):
        assert (SKILL_ROOT / rel).exists(), \
            f"技能目录缺少 {rel}（自包含被破坏：装上去跑不起来）"

    if not _require_plugin_layout("仓库根不得残留技能内容"):
        return
    # 仓库根只留"分发单元"该有的东西，技能内容不得再散落在根上
    for rel in ("SKILL.md", "src", "scripts", "references", ".agents/skills", "requirements.txt"):
        assert not (REPO_ROOT / rel).exists(), \
            f"仓库根不应再出现 {rel}（技能内容应全部在 {name}/ 内）"


def check_frontmatter_portable():
    """frontmatter 只允许跨工具安全字段，且 name 与技能目录名一致。

    跨工具生态里只有 `name` / `description` 是通用必需，`license` / `metadata` 属安全可选；
    `allowed-tools` / `model` / `user-invocable` / `disable-model-invocation` 等是**工具私有**字段，
    写进抬头会让技能在别的平台行为不一致甚至失效（原先的 `compatibility` 也是非标字段，已并入 metadata）。
    """
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md 缺少 YAML frontmatter"
    block = m.group(1)

    keys = re.findall(r"^([A-Za-z0-9_-]+):", block, re.M)
    allowed = {"name", "description", "license", "metadata"}
    extra = [k for k in keys if k not in allowed]
    assert not extra, f"frontmatter 含非跨工具安全字段：{extra}（应并入 metadata）"

    assert re.search(r"^name:\s*video2book\s*$", block, re.M), \
        "frontmatter 的 name 与技能目录名不一致"
    assert re.search(r"^\s+version:\s*\S+$", block, re.M), "metadata 缺少 version"
    # 触发语义：description 要同时说明"做什么"和"什么时候用"，否则跨平台命中率低
    assert "使用本技能" in block or "use this skill" in block.lower(), \
        "description 未写明「何时使用」（跨平台触发依赖它）"


def check_no_private_tool_names():
    """技能正文只讲行动语义，不得写死宿主私有工具名（否则换平台即失效）。

    MCP 工具名 `read_audio` / `read_media` 是跨平台标准（挂了对应 MCP 就能用），不在此列；
    平台差异收在 `references/host-tools/` 里，一个平台一个文件——那是**唯一**允许写私有工具名的位置。
    """
    targets = (
        "SKILL.md",
        "references/delivery_matrix.md",
        "references/install.md",
        "references/host-tools/README.md",
        "references/cli-cookbook.md",
    )
    hits = []
    for rel in targets:
        path = SKILL_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for tool in _PRIVATE_TOOL_NAMES:
            if tool in text:
                hits.append(f"{rel}: {tool}")
    assert not hits, "技能正文写死了宿主私有工具名（应改为行动语义）：\n      " + "\n      ".join(hits)


def check_host_declarations():
    """平台声明层自洽：各平台"装到哪、以什么身份被识别"必须有据可依。

    - Codex：清单必须显式声明 `"skills": "./skills/"`
    - Claude Code：抬头**不含**路径字段（靠插件根 `skills/` 自动发现）
    - 通用 agents marketplace：插件源必须指向本仓库自身
    - 入口说明文件齐备，且**不得用符号链接**（Windows 上 clone 后易退化成普通文件）
    """
    import json as _json

    if not _require_plugin_layout("平台声明层"):
        return

    codex = _json.loads((REPO_ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert codex.get("skills") == "./skills/", f"codex 清单的 skills 路径异常：{codex.get('skills')!r}"
    assert codex.get("name") == SKILL_ROOT.name, "codex 清单的 name 与技能目录名不一致"

    claude = _json.loads((REPO_ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert claude.get("name") == SKILL_ROOT.name, "claude 清单的 name 与技能目录名不一致"
    for forbidden in ("skills", "skillsPath", "skillsDir", "directories"):
        assert forbidden not in claude, \
            f"claude 清单不应声明 {forbidden} 路径字段（Claude Code 靠插件根 skills/ 自动发现）"

    market = _json.loads((REPO_ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    plugins = market.get("plugins") or []
    assert plugins, "通用 agents marketplace 清单缺少 plugins"
    assert (plugins[0].get("source") or {}).get("url") == "./", "marketplace 插件源应指向本仓库自身"

    for rel in ("AGENTS.md", "CLAUDE.md", ".opencode/INSTALL.md"):
        assert (REPO_ROOT / rel).is_file(), f"缺少平台入口/安装说明：{rel}"
    for rel in ("AGENTS.md", "CLAUDE.md"):
        assert not (REPO_ROOT / rel).is_symlink(), f"{rel} 不应是符号链接（Windows 兼容性）"

    # OpenAI 侧声明：字段 schema 未在官方文档证实，保持原样但纳入断言防漂移
    openai_yaml = REPO_ROOT / "agents" / "openai.yaml"
    assert openai_yaml.is_file(), "缺少 agents/openai.yaml（OpenAI 侧声明）"
    text = openai_yaml.read_text(encoding="utf-8")
    for key in ("interface:", "policy:", "dependencies:"):
        assert key in text, f"agents/openai.yaml 缺少 {key}"


def check_host_tools_matrix():
    """工具映射层齐备：`HOST_TARGETS` 里每个平台各一份，且**不得编造工具名**。

    平台文件只对**有实据**的平台保留（清单 schema 有真实原文，或工具名映射已核实）。
    没有实据的平台不进 `HOST_TARGETS`，也不在文档里留「待确认」占位记录——占位记录会让使用者
    以为技能支持它，实装却可能失效（反方向由 `check_no_unverified_platform_traces` 守住）。
    真要支持新平台：先拿到实据，再补 `<平台>.md` 并加进 `HOST_TARGETS`。
    """
    base = SKILL_ROOT / "references" / "host-tools"
    assert base.is_dir(), "缺少 references/host-tools/（工具映射层）"
    assert (base / "README.md").is_file(), "缺少 host-tools/README.md（行动语义基线）"

    install = (SKILL_ROOT / "references/install.md").read_text(encoding="utf-8").lower()
    for host in HOST_TARGETS:
        path = base / f"{host}.md"
        assert path.is_file(), f"缺少 {host} 的工具映射文件"
        text = path.read_text(encoding="utf-8")
        assert "行动语义" in text, f"{host}.md 未声明行动语义对照"
        assert host in install, f"references/install.md 未覆盖平台 {host}"
        if "待确认" in text.splitlines()[0]:
            assert "判断方法" in text, f"{host}.md 标了待确认却没给「判断方法」（不能只写不知道）"


def check_no_unverified_platform_traces():
    """文档层不得出现未证实平台名与外部参照项目名。

    与 `check_host_tools_matrix` 互补：那条保证"该有的平台文件都在"，这条保证"不该出现的平台名
    一个都没有"。**只删不守，下一轮又会被加回来**——把「不保留未证实平台的记录」固化成可复算断言。

    扫描面分两档，**按布局自适应**——技能被单独安装到某平台技能目录时（`~/.claude/skills/…`），
    仓库级文件根本不存在，硬扫会把别人的自检跑成一片红：

    - 任何布局都扫：`SKILL.md` + `references/**/*.md`（技能自带，一定存在）；
    - 仅插件/仓库布局扫：README / 入口文件 / `.opencode/INSTALL.md`（Markdown 层），以及
      `pyproject.toml` / `agents/openai.yaml` / 三个插件清单（**非 Markdown 的声明层**——
      它们的 `description` 同样会被用户读到，不能留成无守区）。

    刻意不扫 `.gitignore` 与本文件自身（豁免理由见 `_UNVERIFIED_TRACE_NAMES` 上方注释）。
    """
    import re

    doc_targets = [SKILL_ROOT / "SKILL.md"]
    doc_targets.extend(sorted((SKILL_ROOT / "references").rglob("*.md")))

    # 仓库级文件只在**确认是本仓库**时才纳入扫描。原因有两层：
    #   ① 技能被装到 `~/.claude/skills/video2book/` 时 `REPO_ROOT` 就是 `~/.claude`，
    #      那里的 `CLAUDE.md` 是**用户自己的全局记忆文件**——扫它等于把用户内容当本仓库痕迹判定；
    #   ② 通用布局探测（`PLUGIN_LAYOUT`）对这种情况会误判为真（它有 `CLAUDE.md` 这一项）。
    # 归属判定用**本仓库特有的清单目录**，它们不会出现在用户的宿主配置根里。
    _REPO_MARKERS = (".codex-plugin", ".claude-plugin", ".agents")
    if any((REPO_ROOT / marker).is_dir() for marker in _REPO_MARKERS):
        doc_targets.extend([
            REPO_ROOT / "README.md",
            REPO_ROOT / "README.en.md",
            REPO_ROOT / "AGENTS.md",
            REPO_ROOT / "CLAUDE.md",
            REPO_ROOT / ".opencode/INSTALL.md",
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "agents" / "openai.yaml",
            REPO_ROOT / ".claude-plugin" / "plugin.json",
            REPO_ROOT / ".codex-plugin" / "plugin.json",
            REPO_ROOT / ".agents" / "plugins" / "marketplace.json",
        ])
    else:
        print("       (非本仓库布局：文档层只扫技能自带文件 SKILL.md + references/)")

    # 边界写法：**不能用 `\b`**。Python 3 的 `\w` 是 Unicode 语义（含 CJK 汉字与下划线），
    # `\bzcode\b` 会漏掉「平台ZCode」「zcode技能」「my_zcode」「zcode1」这些中文文档里最自然的
    # 写法（实测漏报）。改用「左右都不是 ASCII 字母/数字」的显式边界：紧贴汉字、下划线一律命中，
    # 同时仍不误伤 handshake（内含 dsh）、cobra（内含 obra）这类无关词。中文名直接子串匹配。
    patterns = []
    for name in _UNVERIFIED_TRACE_NAMES:
        if name.isascii():
            patterns.append(re.compile(
                rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", re.IGNORECASE))
        else:
            patterns.append(re.compile(re.escape(name)))

    scanned = 0
    hits = []
    for path in doc_targets:
        if not path.is_file():
            continue
        scanned += 1
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(pattern.search(line) for pattern in patterns):
                hits.append(f"{rel}:{lineno}: {line.strip()[:90]}")

    # 下限哨兵：`SKILL.md` + `references/` 若干文档是最小可信集合（单独安装时也存在）。
    # 不写死成「全部 12 个文档」——那会让单独安装的合法布局必然 FAIL（`PLUGIN_LAYOUT` 已判过）。
    assert (SKILL_ROOT / "SKILL.md").is_file(), "缺少 SKILL.md，扫描范围无从谈起"
    assert scanned >= 5, f"扫描范围异常，只找到 {scanned} 个文档文件"
    assert not hits, (
        "文档层出现未证实平台名或外部参照项目名（不要留「待确认」式记录）：\n      "
        + "\n      ".join(hits)
        + "\n      如需支持新平台：先取得实据，再补 references/host-tools/<平台>.md，"
          "加进 HOST_TARGETS，并从 _UNVERIFIED_TRACE_NAMES 移除同名条目（两步都要做）。"
    )

    # Gemini 刻意**不在**禁用名单里：它同时是合法的**模型口径**名（音频模态示例、音频 token
    # 系数标定），删掉会让音频预算失去依据。因此单独钉住"它不作为宿主平台出现"——根入口文件不得再有。
    assert not (REPO_ROOT / "GEMINI.md").exists(), \
        "GEMINI.md 不应存在（Gemini 不作为宿主平台声明；模型口径的 Gemini 表述另行保留）"


def check_task_file_export_end_to_end():
    """在临时工作区实证块级任务书导出门禁按预期收敛。"""
    import json
    import tempfile

    from src.core.pipeline import export_block_article_task
    from src.core.workspace import (
        TaskWorkspace,
        find_module_article,
        module_article_path,
    )
    from src.generator.topic_planner import SemanticTopicPlanner as P

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="selfcheck_task", base_dir=tmp)
        block = {"block_id": 1, "title": "绪论与数制", "span": "P01-P02", "episodes": [1, 2],
                 "duration_min": 46.0, "audio": "audio/_blocks/探针_01_绪论与数制(P01-P02).m4a"}
        parts = [{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}]

        # 1) 逐字稿未就绪也照常导出任务书：第 1 节前置条件要求执行者立即停止并回报
        task = export_block_article_task(ws, block, None, course_title="测试课程", article_type="学习")
        assert task.name == "模块01_绪论与数制_TASK.md", task.name
        assert "**未就绪**" in task.read_text(encoding="utf-8"), "未就绪态未在任务书上写明"

        # 2) 逐字稿落盘后，任务书写明语料已就绪与字节数
        transcript = ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md"
        transcript.write_text("[00:00:00] 语料。" + chr(10) * 60, encoding="utf-8")
        task = export_block_article_task(ws, block, transcript,
                                         course_title="测试课程", article_type="学习")
        assert "已就绪" in task.read_text(encoding="utf-8"), "就绪态未在任务书上写明"

        # 3) 模块长文就绪判定：按 模块XX_ 前缀宽容定位，空壳不算
        target = module_article_path(ws.articles_dir, block)
        assert find_module_article(ws.articles_dir, block) is None, "尚无长文却报告已就绪"
        target.write_text("正文" * 400, encoding="utf-8")
        assert find_module_article(ws.articles_dir, block) == target, "模块长文未被宽容定位到"

        # 4) 归并：缺 note_plan.json 时导出归并任务书并返回 None（工具层不做本地归并）
        assert P.notes([block], parts, course_title="测试课程", ws=ws) is None, \
            "无归并时应返回 None 而非本地兜底结果"
        assert (ws.root_dir / "note_plan_TASK.md").exists(), "NOTE_PLAN_TASK 未落盘"
        assert not (ws.root_dir / "note_plan.json").exists(), "工具层不得创建 note_plan.json"
        assert not (ws.root_dir / "topic_plan_TASK.md").exists(), "第一趟规划任务书不得回流"

        # 5) Agent 产出合法归并后必须被采纳；非法归并必须被拒绝
        (ws.root_dir / "note_plan.json").write_text(json.dumps(
            [{"note_id": 1, "note_title": "绪论与数制", "blocks": [1], "core_theme": "x"}],
            ensure_ascii=False), encoding="utf-8")
        notes = P.notes([block], parts, course_title="测试课程", ws=ws)
        assert notes is not None and len(notes) == 1, "合法归并未被采纳"

        (ws.root_dir / "note_plan.json").write_text(json.dumps(
            [{"note_id": 1, "note_title": "残缺", "blocks": []}], ensure_ascii=False),
            encoding="utf-8")
        assert P.notes([block], parts, course_title="测试课程", ws=ws) is None, \
            "未认领任何块的非法归并未被拒绝"


def check_stage1_gate_ignores_task_files():
    """阶段一门禁以**块**为单位：任务书不得计入已完成，无块清单的工作区不判定。"""
    import json
    import tempfile

    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    from queue_tracker import scan_status
    from src.core.workspace import TaskWorkspace

    def _装块(ws, blocks):
        block_dir = ws.audio_dir / "_blocks"
        block_dir.mkdir(parents=True, exist_ok=True)
        (block_dir / "blocks.json").write_text(json.dumps(
            {"version": 2, "target_minutes": 50.0, "min_minutes": 40.0, "max_minutes": 60.0,
             "effective_limit_minutes": 75.0, "course_short": "探针", "noop": False,
             "input_signature": "probe", "blocks": blocks}, ensure_ascii=False), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="gate_probe", base_dir=tmp)
        ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])

        # 0) 无块清单：不做阶段一判定（旧链路按逐集长文判进度，那种产物已不存在）
        (ws.articles_dir / "模块01_绪论与数制_TASK.md").write_text("提示词" * 800, encoding="utf-8")
        st = scan_status(ws.root_dir)
        assert st["stage1_unit"] == "none", f"无块清单却给出了判定单位: {st['stage1_unit']}"
        assert st["blocks_total"] == 0 and not st["is_stage1_complete"], "无块清单时门禁不得放行"

        # 1) 装块但没有模块长文：任务书体积远超 1000 字节门禁，不得被误判为已交长文
        _装块(ws, [{"block_id": 1, "title": "绪论与数制", "span": "P01-P02",
                    "episodes": [1, 2], "duration_min": 46.0,
                    "audio": "audio/_blocks/探针_01_绪论与数制(P01-P02).m4a"}])
        st2 = scan_status(ws.root_dir)
        assert st2["blocks_total"] == 1 and st2["blocks_done"] == [], f"任务书被误判为长文: {st2}"
        assert not st2["is_stage1_complete"], "尚无模块长文时阶段一门禁不得放行"

        # 2) 空壳长文（< 1000 字节）单独报出来，仍不放行
        (ws.articles_dir / "模块01_绪论与数制_精读长文.md").write_text("短", encoding="utf-8")
        st3 = scan_status(ws.root_dir)
        assert 1 in st3["invalid_articles"], "空壳模块长文未进异常清单"
        assert not st3["is_stage1_complete"], "空壳长文不得放行"

        # 3) 达标长文落盘后才计入，且覆盖的集号随之标记完成
        (ws.articles_dir / "模块01_绪论与数制_精读长文.md").write_text("正文" * 400, encoding="utf-8")
        st4 = scan_status(ws.root_dir)
        assert len(st4["blocks_done"]) == 1 and st4["is_stage1_complete"], f"达标长文未被计入: {st4}"
        assert st4["completed_count"] == 2 and st4["pending_count"] == 0, \
            f"块覆盖的集号未被标记完成: {st4['completed_count']}/{st4['pending_count']}"


def check_dedup_reuses_without_subtitles():
    """重复集只复用**已切出的分集逐字稿**：块级长文按块产出，没有「一集一篇」可复用。"""
    import tempfile

    from src.core.workspace import TaskWorkspace

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="dedup_probe", base_dir=tmp)
        for name in ("P01_绪论.m4a", "P02_绪论重复.m4a"):
            (ws.audio_dir / name).write_bytes(b"x" * 20000)

        # 主集已切出分集逐字稿（可选产物）；任务书同目录同前缀，必须被排除在复用源之外
        transcript = "这是一份正式逐字稿。" * 100
        (ws.subtitles_dir / "P01_绪论_逐字稿.md").write_text(transcript, encoding="utf-8")
        (ws.subtitles_dir / "P01_绪论_转录任务书.md").write_text("这是任务书。" * 100, encoding="utf-8")

        synced = ws.sync_duplicate_assets()
        assert len(synced) == 1, f"重复分集未被同步: {synced}"
        assert synced[0]["synced_transcript"], f"分集逐字稿未复用: {synced[0]}"
        dst = ws.subtitles_dir / "P02_绪论重复_逐字稿.md"
        assert dst.exists(), "P02 分集逐字稿未复用"
        assert dst.read_text(encoding="utf-8") == transcript, "复用源不是正式逐字稿（任务书被误拷）"

        # 没有分集逐字稿时不报同步：块级链路不靠它派发
        (ws.subtitles_dir / "P01_绪论_逐字稿.md").unlink()
        dst.unlink()
        assert ws.sync_duplicate_assets() == [], "无逐字稿源时不应报告同步"


def check_sessdata_store_safety():
    """SESSDATA 持久化：往返一致、脱敏不泄露，且存档路径必须落在版本库之外。"""
    import subprocess as _sp
    import tempfile

    from src.core.credentials import DEFAULT_STORE_NAME, SessdataStore, resolve_sessdata, store_path

    secret = "abc123def456ghi789"

    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "store.json"

        assert SessdataStore.load(path=store) is None, "无存档时不得凭空返回凭证"
        try:
            SessdataStore.save("   ", path=store)
            raise AssertionError("空 SESSDATA 未被拒绝")
        except ValueError:
            pass

        SessdataStore.save(secret, path=store)
        assert SessdataStore.load(path=store) == secret, "存档往返不一致"
        assert secret not in SessdataStore.mask(secret), "脱敏展示泄露了完整凭证"

        assert SessdataStore.clear(path=store) is True
        assert SessdataStore.clear(path=store) is False, "重复清除应返回 False"
        assert SessdataStore.load(path=store) is None

    # 显式传入优先于本地存档；空白视作未传入
    assert resolve_sessdata(secret) == secret, "显式传入未优先生效"
    assert resolve_sessdata("   ") == SessdataStore.load(), "空白显式值应回退到本地存档"

    # 抖音 Cookie 存档与 SESSDATA 同构，必须独立走一遍读写往返 + 安全网
    from src.core.credentials import (
        DEFAULT_DOUYIN_STORE_NAME,
        DouyinCookieStore,
        douyin_store_path,
        resolve_douyin_cookie,
    )

    with tempfile.TemporaryDirectory() as tmp:
        dy_store = Path(tmp) / "douyin.json"

        assert DouyinCookieStore.load(path=dy_store) is None, "无存档时不得凭空返回抖音凭证"
        try:
            DouyinCookieStore.save("   ", path=dy_store)
            raise AssertionError("空抖音 Cookie 未被拒绝")
        except ValueError:
            pass

        DouyinCookieStore.save(secret, path=dy_store)
        assert DouyinCookieStore.load(path=dy_store) == secret, "抖音存档往返不一致"
        assert secret not in DouyinCookieStore.mask(secret), "抖音脱敏展示泄露了完整凭证"

        assert DouyinCookieStore.clear(path=dy_store) is True
        assert DouyinCookieStore.clear(path=dy_store) is False, "重复清除应返回 False"
        assert DouyinCookieStore.load(path=dy_store) is None

    assert resolve_douyin_cookie(secret) == secret, "抖音显式传入未优先生效"
    assert douyin_store_path().name == DEFAULT_DOUYIN_STORE_NAME
    assert douyin_store_path().parent == PRODUCTS_ROOT, \
        f"抖音凭证存档不在产物根: {douyin_store_path()}"
    # 两类凭证必须各占一个文件，否则后写会覆盖先写
    assert store_path() != douyin_store_path(), "两类凭证共用同一存档文件，会互相覆盖"

    # 默认存档路径：必须落在产物根，且 skill/.gitignore 留有安全网。
    # 两类凭证（B 站 SESSDATA / 抖音 Cookie）一并纳入，避免新增一类时漏掉安全网。
    _凭证文件 = ((DEFAULT_STORE_NAME, store_path()),
                 (DEFAULT_DOUYIN_STORE_NAME, douyin_store_path()))
    if PLUGIN_LAYOUT:
        _ignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for _name, _p in _凭证文件:
            assert _name in _ignore_text, f"{_name} 未被仓库 .gitignore 覆盖（安全网缺失）"
    for _name, _p in _凭证文件:
        assert _p.name == _name
        assert _p.parent == PRODUCTS_ROOT, \
            f"凭证存档不在产物根: {_p} (期望目录 {PRODUCTS_ROOT})"
    if not _HAS_GIT:
        print("       (未找到 git 命令，跳过「凭证未入库」的 git 校验)")
        return
    for repo in (REPO_ROOT, MCP_REPO_BASE):
        if not (repo / ".git").is_dir():
            continue
        for _name, _p in _凭证文件:
            tracked = run_quiet(
                ["git", "ls-files", "--", _name],
                cwd=str(repo), stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, timeout=60,
            )
            assert not tracked.stdout.strip(), \
                f"凭证存档已进入 {repo.name} 版本控制: {tracked.stdout.strip()}"
            if _paths.is_container_layout():
                try:
                    _p.relative_to(repo)
                except ValueError:
                    continue
                raise AssertionError(f"凭证存档位于 {repo.name} 仓库工作树内")


def check_cache_paths_anchored():
    """缓存/凭证文件路径必须锚定**产物根**，不得随当前所在目录漂移，也不得落回代码仓库。"""
    from src.core.credentials import douyin_store_path, store_path
    from src.core.wbi import WbiSigner

    for label, p in (("WBI 密钥", WbiSigner._解析密钥文件路径()),
                     ("凭证存档", store_path()),
                     ("抖音凭证存档", douyin_store_path())):
        assert p.is_absolute(), f"{label}路径不是绝对路径: {p}"
        assert PRODUCTS_ROOT in p.parents, f"{label}路径未锚定产物根: {p}"
        if PLUGIN_LAYOUT and _paths.is_container_layout():
            assert REPO_ROOT not in p.parents, f"{label}路径落在了代码仓库内: {p}"

    # 显式传入的相对路径按**容器根**解析（兼容拆分前的 `output/.wbi_keys.json` 写法）。
    # 注意这是字面路径语义：`$BVB_OUTPUT_DIR` 覆盖产物根时，两者的落点并不相同。
    legacy = WbiSigner._解析密钥文件路径("output/.wbi_keys.json")
    assert legacy == HOME_ROOT / "output" / ".wbi_keys.json", f"旧式相对路径解析异常: {legacy}"


def check_render_compat_rules():
    """交付物渲染兼容约束在位：Typora 优先，字符画必须进围栏，禁用 GitHub 告警块。"""
    from src.generator.block_synthesizer import BlockSynthesizer
    from src.generator.prompt_templates import (
        ARTICLE_LEARNING_PROMPT,
        RENDER_COMPAT_RULES,
    )

    # 1) 共享规则是唯一文案来源，两条硬约束都必须在
    assert "```text" in RENDER_COMPAT_RULES, "共享规则缺少「字符画必须进围栏」硬约束"
    assert "成对闭合" in RENDER_COMPAT_RULES, "共享规则缺少「围栏必须成对闭合」硬约束"
    assert "GitHub 专有" in RENDER_COMPAT_RULES, "共享规则缺少「禁用 GitHub 告警块」禁令"

    # 2) 讲义提示词已注入规则，且不再处方 GitHub 告警块
    assert RENDER_COMPAT_RULES in ARTICLE_LEARNING_PROMPT, "讲义提示词未注入渲染兼容规则"
    assert "（如 `> [!TIP]`）" not in ARTICLE_LEARNING_PROMPT, "讲义提示词仍在处方 GitHub 告警块"
    for ph in ("{title}", "{part_title}", "{content}"):
        assert ph in ARTICLE_LEARNING_PROMPT, f"讲义提示词占位符缺失: {ph}"

    # 3) 模块笔记提示词自带字符画围栏要求（换版后【排版】一节覆盖渲染硬约束）
    from src.generator.prompt_templates import MODULE_NOTE_PROMPT
    assert "```text" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 未要求字符画进围栏"
    assert "{article_list}" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少语料清单占位符"

    # 4) 笔记只有一种风格；渲染硬约束**不再重复追加**（【排版】一节已覆盖同一批要求）
    import inspect as _inspect

    assert "style" not in _inspect.signature(BlockSynthesizer.build_synthesis_prompt).parameters, \
        "build_synthesis_prompt 不应再有 style 参数（八种旧风格已删除）"
    block_meta = {"block_id": 1, "block_title": "t", "episodes": [1], "core_theme": "x"}
    rendered = BlockSynthesizer.build_synthesis_prompt(block_meta, [])
    assert RENDER_COMPAT_RULES not in rendered, \
        "笔记提示词不得再追加 RENDER_COMPAT_RULES（【排版】已覆盖，说两遍只会稀释重点）"
    assert "【排版】" in rendered, "笔记提示词未注入【排版】一节"

    # 5) 格式总纲必须写明阅读器为 Typora
    rel = "references/delivery_matrix.md"
    text = (SKILL_ROOT / rel).read_text(encoding="utf-8")
    assert "Typora" in text, f"{rel} 未声明 Typora 阅读场景"
    assert "```text" in text, f"{rel} 未写入字符画围栏要求"


def check_module_note_contract():
    """模块笔记契约：文章直供 + 只写结论 + 零套话 + 版式规范 + 两条排版硬约束 + 任务书回收。"""
    from src.core.task_cleanup import cleanup_completed_tasks  # noqa: F401  (导入即校验依赖无环)
    from src.core.workspace import TaskWorkspace
    from src.generator.block_synthesizer import BlockSynthesizer
    from src.generator.prompt_templates import (
        MODULE_NOTE_PROMPT,
        NOTE_VISUAL_SPEC,
    )

    # 1) 专属提示词必须点名禁止「套话填充」「分集标题」「分集口吻」「中途截断」
    for 关键短语, 说明 in (
        ("概念属性与边界", "套话黑名单"),
        ("严禁用分集编号或分集标题作标题", "分集标题禁令"),
        ("上一讲", "分集口吻禁令"),
        ("不中途截断", "截断禁令"),
    ):
        assert 关键短语 in MODULE_NOTE_PROMPT, f"MODULE_NOTE_PROMPT 缺少{说明}：{关键短语}"

    # 2) 【排版】标志性要求必须在位：成品只有 H1 + 分节条目，且来源标注彻底不许出现
    assert "不写目录、元信息引用块、知识拓扑树、节级主旨句、速查卡、总纲" in NOTE_VISUAL_SPEC, \
        "【排版】未写明「成品只有 H1 + 按知识主题分节的条目」"
    assert "来源: P03" not in NOTE_VISUAL_SPEC, "【排版】仍残留来源标注样例"
    assert "```text" in NOTE_VISUAL_SPEC, "【排版】未要求字符画进围栏"
    assert "字符画写在列表项里时，围栏整体缩进 4 空格" in NOTE_VISUAL_SPEC, \
        "【排版】缺少「列表项内围栏缩进 4 空格」纪律"

    # 2b) 标题纪律：层级最多到 `####`，且**不写序号**（阅读器会自动编号，手写会叠字）；
    #      总量**不用数字配额**——写死配额会诱导模型机械凑数或粗暴削内容，反而失真。
    assert "最多到 `####`" in MODULE_NOTE_PROMPT, "提示词未写明标题层级上限（最多到 ####）"
    assert "标题里一律不要写序号" in MODULE_NOTE_PROMPT, "提示词缺少「标题不写序号」的要求"
    for 数字配额 in ("8~12 节", "20~45 个", "2~3 条要点"):
        assert 数字配额 not in MODULE_NOTE_PROMPT, f"提示词不得写死数字配额：{数字配额}"
    # 旧版「只允许二级标题」随换版作废：留着会与「最多到 ####」正面打架
    assert "不得出现 `###` 及更深的标题" not in MODULE_NOTE_PROMPT, \
        "提示词回流了旧版「禁止 ###」措辞（与「最多到 ####」冲突）"

    # 2c) 骨架与内容纪律：嵌套 `*` 骨架、只写结论、标题朴素
    assert "逐级 4 空格缩进的 `*` 列表" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少嵌套 * 骨架说明"
    assert "只写结论" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少「只写结论」纪律"
    assert "标题只写术语或名词短语" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少朴素标题要求"
    assert "每个 `###` 至少两条条目" in MODULE_NOTE_PROMPT, \
        "MODULE_NOTE_PROMPT 缺少「每个 ### 至少两条条目」要求"
    assert "```text" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少围栏要求"

    # 2d) 事实边界：禁外部知识，缺口写「长文未说明」（换版后不再有补充规范入口）
    assert "禁止引入外部知识" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少「禁止引入外部知识」"
    assert "长文未说明" in MODULE_NOTE_PROMPT, "MODULE_NOTE_PROMPT 缺少「长文未说明」缺口约定"
    for 已取消 in ("NOTE_SUPPLEMENT_RULES", "六、补充规范", "严格限量，宁缺勿补"):
        assert 已取消 not in MODULE_NOTE_PROMPT, \
            f"补充规范已随换版取消，提示词仍残留：{已取消}"

    # 2e) 结构门禁必须与提示词同步：标题序号与层级上限都要有可复算的检查项
    from src.core.deliverable_lint import STRUCTURE_KEYS as _STRUCTURE_KEYS
    from src.core.deliverable_lint import check_note_structure as _check_note_structure
    for key in ("has_h1", "has_sections", "no_h5plus_headings",
                "no_numbered_headings", "no_redundant_tail"):
        assert key in _STRUCTURE_KEYS, f"STRUCTURE_KEYS 缺少结构项：{key}"
    assert "has_metadata_block" not in _STRUCTURE_KEYS, \
        "抬头元信息引用块已取消，不得再作为结构项"
    合规 = _check_note_structure(
        "# 标题\n\n## 主题一\n\n* **术语**\n\n    * 条目一。\n\n### 子题\n\n    * 条目二。\n\n"
        "#### 更深一层\n\n    * 条目三。\n\n## 主题二\n\n    * 条目四。\n"
    )
    assert 合规["no_h5plus_headings"] and 合规["no_numbered_headings"], f"合规笔记被误判：{合规}"
    违规 = _check_note_structure("# 标题\n\n## 1. 手写序号\n\n#### 可以\n\n##### 太深\n")
    assert not 违规["no_numbered_headings"], "带手写序号的标题未被检出（会导致与阅读器编号叠字）"
    assert not 违规["no_h5plus_headings"], "`#####` 未被检出（笔记标题上限应为 ####）"

    # 3) 旧版八种笔记风格必须已彻底删除（含标签与指令文案）
    for 已删除 in ("NOTE_STYLES", "minimal", "detailed", "academic", "tutorial",
                  "task_oriented", "business", "meeting_minutes", "life_journal"):
        text = (SKILL_ROOT / "src" / "generator" / "prompt_templates.py").read_text(encoding="utf-8")
        assert 已删除 not in text, f"prompt_templates.py 仍残留旧笔记风格痕迹：{已删除}"

    # 4) 任务书渲染：必须带上【排版】一节与语料清单
    #    注：不能用 `NOTE_VISUAL_SPEC in prompt` 判定——它内含 `{block_title}` 占位符，
    #    渲染时已被替换成真实主题名，整段字面串必然对不上。
    block_meta = {"block_id": 3, "block_title": "关系数据库", "episodes": [6, 7], "core_theme": "关系模型"}
    样例文章 = SKILL_ROOT / "SKILL.md"  # 仅需一个存在的文件来渲染字节数
    prompt = BlockSynthesizer.build_synthesis_prompt(block_meta, [样例文章])
    assert "【排版】" in prompt, "任务书未注入【排版】一节"
    assert "字符画写在列表项里时，围栏整体缩进 4 空格" in prompt, "任务书未注入围栏缩进纪律"
    assert "# 关系数据库" in prompt, "H1 占位符未被替换为真实主题名"
    assert "SKILL.md" in prompt, "任务书未渲染语料清单"

    # 5) 语料清单渲染的是**模块长文**的绝对路径与字节数；知识元索引已随逐集链路移除
    assert "模块长文" in prompt, "笔记语料应写明是模块长文"
    assert "SKILL.md" in prompt and "字节" in prompt, "任务书未渲染语料清单"
    assert "可选结构化索引" not in prompt, "知识元索引已随逐集链路移除，不得回加"

    # 6) 任务书回收：成品已产出才回收，每类保留 1 份范本，未产出的一律保留
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="cleanup_task", base_dir=tmp)

        # 模块长文：模块01 无成品（保留待办 + 范本）、模块02 有成品（回收）
        (ws.articles_dir / "模块01_绪论_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.articles_dir / "模块02_数制_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.articles_dir / "模块02_数制_精读长文.md").write_text("内容" * 400, encoding="utf-8")
        # 笔记任务书：笔记01 有成品（范本，保留）、笔记02 有成品（回收）、笔记03 无成品（保留）
        (ws.notes_dir / "笔记01_绪论_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.notes_dir / "笔记02_关系_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.notes_dir / "笔记03_理论_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.notes_dir / "笔记01_绪论_笔记.md").write_text("笔记" * 600, encoding="utf-8")
        (ws.notes_dir / "笔记02_关系_笔记.md").write_text("笔记" * 600, encoding="utf-8")
        # 块级转录任务书：BLK01 有成品（范本，保留）、BLK02 有成品（回收）、
        # BLK03 的成品是空文件（不算成品，保留待办）
        for name in ("BLK01_P01_转录任务书.md", "BLK02_P02_转录任务书.md", "BLK03_P03_转录任务书.md"):
            (ws.subtitles_dir / name).write_text("t" * 200, encoding="utf-8")
        (ws.subtitles_dir / "BLK02_P02_逐字稿.md").write_text("逐字稿" * 200, encoding="utf-8")
        (ws.subtitles_dir / "BLK03_P03_逐字稿.md").write_text("", encoding="utf-8")

        result = cleanup_completed_tasks(ws, keep_per_category=1, dry_run=False)
        remaining = sorted(p.name for p in ws.articles_dir.glob("*_TASK.md"))
        assert remaining == ["模块01_绪论_TASK.md"], f"模块长文任务书回收结果异常: {remaining}"
        note_remaining = sorted(p.name for p in ws.notes_dir.glob("*_TASK.md"))
        assert note_remaining == ["笔记01_绪论_TASK.md", "笔记03_理论_TASK.md"], \
            f"笔记任务书回收结果异常: {note_remaining}"
        transcript_remaining = sorted(p.name for p in ws.subtitles_dir.glob("BLK*_转录任务书.md"))
        assert transcript_remaining == ["BLK01_P01_转录任务书.md", "BLK03_P03_转录任务书.md"], \
            f"块级转录任务书回收结果异常: {transcript_remaining}"
        assert len(result["deleted"]) == 3, f"回收数量异常: {result['deleted']}"
        assert set(result["counts"]) == {"articles", "notes", "transcripts"}, \
            f"回收类别异常: {result['counts']}"



def check_article_prompt_types():
    """长文提示词风格契约：学习（推荐）+ 旧版（原稳定版）两种风格，由用户确认后使用。

    另外四种视频形态只登记、不提供提示词：命中即打印风格菜单并终止任务（不猜、不降级）。
    """
    import tempfile

    from src.core.pipeline import export_block_article_task
    from src.core.workspace import TaskWorkspace
    from src.generator.prompt_templates import (
        ARTICLE_LEARNING_PROMPT,
        ARTICLE_LEGACY_PROMPT,
        ARTICLE_PROMPT_TYPES,
        IMPLEMENTED_ARTICLE_TYPES,
        ArticlePromptTypeError,
        render_article_prompt_menu,
        resolve_article_prompt,
    )

    # 1) 只提供两种风格，且「学习」是推荐风格；其余形态登记齐备但必须没有提示词
    assert IMPLEMENTED_ARTICLE_TYPES == ("learning", "legacy"),         f"已提供提示词的状态异常: {IMPLEMENTED_ARTICLE_TYPES}"
    assert ARTICLE_PROMPT_TYPES["learning"].get("recommended") is True, "「学习」未被标为推荐风格"
    assert len(ARTICLE_PROMPT_TYPES) >= 2, "风格矩阵至少应登记学习与旧版"
    for key, meta in ARTICLE_PROMPT_TYPES.items():
        assert meta.get("label"), f"风格 {key} 缺少中文名"
        assert meta.get("signals"), f"风格 {key} 缺少适用信号（用户无法据以选择）"
        if key not in IMPLEMENTED_ARTICLE_TYPES:
            assert not meta.get("prompt"), f"风格 {key} 不应提供提示词"

    # 2) 旧版必须与改写前的原稳定版一致：仍走客观学术第一视角与随堂自测
    for 关键短语 in ("客观、直接的技术/学术第一视角", "随堂自测", "去口语化"):
        assert 关键短语 in ARTICLE_LEGACY_PROMPT, f"旧版提示词缺少原有条款：{关键短语}"
    for 反例 in ("保住讲师的讲课风格", "标题用技术文档的朴素写法"):
        assert 反例 not in ARTICLE_LEGACY_PROMPT, f"旧版提示词混入了新风格条款：{反例}"

    # 3) 学习版（推荐）的立意必须在位：保讲课风格 / 高信息密度 / 成稿观感 / 噪声清单含舞台提示
    for 关键短语 in ("保住讲师的讲课风格", "高信息密度", "成稿观感", "（笑）"):
        assert 关键短语 in ARTICLE_LEARNING_PROMPT, f"学习版提示词缺少「{关键短语}」"

    # 4) 已判定不合格的扩张型/编造型条款不得回流
    for 反例 in ("宁可充分展开", "绝不跳步"):
        assert 反例 not in ARTICLE_LEARNING_PROMPT, f"学习版提示词回流了扩张型条款：{反例}"
    assert "不要凭印象替他补一份" in ARTICLE_LEARNING_PROMPT, "缺少「不得替讲师补写代码」的禁令"

    # 5) 标题规则（第 4~8 轮逐步加固）：朴素写法 + 数量与切分跟着内容 + 不许硬造子标题 + 不许撑大原文
    for 关键短语 in ("标题用技术文档的朴素写法", "严禁口语化、修辞化、带语气或带悬念的标题",
                   "标题的数量与切分跟着这一讲走", "不许硬造子标题", "不许出现空壳层级"):
        assert 关键短语 in ARTICLE_LEARNING_PROMPT, f"学习版提示词缺少标题规则：{关键短语}"
    assert "不构成" in ARTICLE_LEARNING_PROMPT and "义务" in ARTICLE_LEARNING_PROMPT, \
        "学习版提示词未禁止「立了标题就要写满」"

    # 6) 风格解析：键 / 中文名都要命中；未指定、拼错、无提示词的形态都必须终止
    assert resolve_article_prompt("learning")["key"] == "learning"
    assert resolve_article_prompt("学习")["key"] == "learning"
    assert resolve_article_prompt("legacy")["key"] == "legacy"
    assert resolve_article_prompt("旧版")["key"] == "legacy"
    for bad in ("", "网课", "consulting", "livestream"):
        try:
            resolve_article_prompt(bad)
        except ArticlePromptTypeError as err:
            assert "菜单" in err.report, "终止提示未附带风格菜单"
            continue
        raise AssertionError(f"非预设风格未终止任务: {bad!r}")

    # 7) 菜单必须列出全部风格、标出推荐、并给出可复制用法
    menu = render_article_prompt_menu()
    for key in ARTICLE_PROMPT_TYPES:
        assert f"--article-type {key}" in menu, f"风格菜单缺少 {key}"
    assert "推荐" in menu, "风格菜单未标出推荐风格"
    assert "--all --article-type" in menu, "风格菜单缺少可复制用法"

    # 8) 端到端：未命中风格不得落盘任何任务书；命中时任务书须写明风格、注入对应提示词，
    #    并把**块级逐字稿**写成「唯一事实来源」（这就是块级链路对写作角色的全部约束）
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="style_gate", base_dir=tmp)
        block = {"block_id": 1, "title": "绪论与数制", "span": "P01-P02", "episodes": [1, 2],
                 "duration_min": 46.0, "audio": "audio/_blocks/测试课_01_绪论与数制(P01-P02).m4a"}
        for bad in ("", "consulting", "乱写"):
            try:
                export_block_article_task(ws, block, None, course_title="测试课程", article_type=bad)
            except ArticlePromptTypeError:
                pass
            else:
                raise AssertionError(f"风格 {bad!r} 未被门禁拦下")
        assert not list(ws.articles_dir.glob("*_TASK.md")), "风格未命中却落了任务书"

        ws.subtitles_dir.mkdir(parents=True, exist_ok=True)
        transcript = ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md"
        transcript.write_text("# 逐字稿" + chr(10) + chr(10) + "[00:00:00] 开场白。" + chr(10),
                              encoding="utf-8")

        task = export_block_article_task(ws, block, transcript, course_title="测试课程",
                                         article_type="学习", page_titles={1: "绪论", 2: "数制"})
        text = task.read_text(encoding="utf-8")
        assert task.name == "模块01_绪论与数制_TASK.md", task.name
        assert "长文风格：学习" in text, "任务书未写明所选长文风格"
        assert "保住讲师的讲课风格" in text, "任务书未注入学习版提示词"
        for 关键短语 in ("唯一事实来源", "逐字稿未就绪", "所属块音频（备查，不必再听）",
                       "P01 绪论", "P02 数制"):
            assert 关键短语 in text, f"任务书缺少块级链路条款：{关键短语}"
        for 已移除 in ("待听音切片清单", "read_audio", "本集逐字稿"):
            assert 已移除 not in text, f"任务书回流了旧链路产物：{已移除}"

        legacy_task = export_block_article_task(ws, block, transcript, course_title="测试课程",
                                                article_type="legacy")
        legacy_text = legacy_task.read_text(encoding="utf-8")
        assert "长文风格：旧版" in legacy_text, "旧版任务书未写明风格"
        assert "随堂自测" in legacy_text, "旧版任务书未注入旧版提示词"


def check_deliverable_lint_gate():
    """真实交付物机器门禁：告警块与围栏配对必须为 0（仓库无 output/ 时自动跳过）。

    这是「只在提示词里喊口号、没人验货」的补丁：提示词规则容易被改回，产物指标不会说谎。
    """
    from src.core import fsutil
    from src.core.deliverable_lint import lint_render, summarize_render
    from src.core.task_cleanup import find_workspaces

    workspaces = find_workspaces(PRODUCTS_ROOT)
    if not workspaces:
        print("       (仓库内无 output/ 工作区，跳过真实产物门禁)")
        return

    alerts = unbalanced = 0
    for ws in workspaces:
        # 递归走 fsutil：工作区里若混入 Windows 不受信任的装入点，rglob 会整体抛 OSError。
        for path in fsutil.iter_files(ws.root_dir, "*.md", skip_hidden_dirs=True):
            try:
                rel_parents = path.relative_to(ws.root_dir).parts[:-1]
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel_parents):
                continue  # 归档/备份目录不计入
            if path.name.endswith(("_TASK.md", "_KERNEL_TASK.md")):
                continue
            try:
                summary = summarize_render(lint_render(path.read_text(encoding="utf-8")))
            except OSError:
                continue
            alerts += summary["alert_blocks"]
            unbalanced += summary["fences_unbalanced"]

    assert alerts == 0, f"交付物中仍存在 {alerts} 处 GitHub 告警块（> [!TIP] 等）"
    assert unbalanced == 0, f"交付物中仍有 {unbalanced} 个未成对闭合的代码围栏"


def check_docs_style_matrix_clean():
    """文档不得再残留已删除的旧笔记风格：minimal / detailed 只存在于历史记忆里。"""
    # SKILL.md 与 references 随技能目录走；README 留在插件根（单独安装时不存在）
    pairs = [("SKILL.md", SKILL_ROOT), ("references/delivery_matrix.md", SKILL_ROOT)]
    if PLUGIN_LAYOUT:
        pairs += [("README.md", REPO_ROOT), ("README.en.md", REPO_ROOT)]
    for rel, root in pairs:
        text = (root / rel).read_text(encoding="utf-8")
        low = text.lower()
        for 已删除 in ("minimal", "detailed"):
            assert 已删除 not in low, f"{rel} 仍残留已删除的笔记风格字样：{已删除}"


def check_delivery_matrix_article_types():
    """交付矩阵的长文类型表必须覆盖全部已登记类型，且标明 legacy 的存在与 learning 的推荐地位。"""
    from src.generator.prompt_templates import ARTICLE_PROMPT_TYPES, IMPLEMENTED_ARTICLE_TYPES

    rel = "references/delivery_matrix.md"
    text = (SKILL_ROOT / rel).read_text(encoding="utf-8")
    for key in ARTICLE_PROMPT_TYPES:
        assert f"`{key}`" in text, f"{rel} 的类型表缺少 {key} 行"
    assert "推荐" in text or "已提供（推荐" in text, f"{rel} 未标出推荐风格"
    assert set(IMPLEMENTED_ARTICLE_TYPES) == {"learning", "legacy"}, \
        f"已提供提示词的类型集合变化，文档需同步：{IMPLEMENTED_ARTICLE_TYPES}"


def check_version_consistency():
    """版本号必须处处一致：SKILL 抬头 / pyproject / src.__version__ / 各平台清单。"""
    import re as _re

    import json
    import src

    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    m_skill = _re.search(r"^\s*version:\s*([^\s]+)\s*$", skill, _re.M)
    assert m_skill, "SKILL.md 抬头缺少 version 字段"

    # 技能级一致性：无论装在哪，SKILL.md 抬头与 src.__version__ 都必须一致
    versions = {"SKILL.md": m_skill.group(1), "src.__version__": src.__version__}

    # 仓库级一致性：pyproject 与各平台清单（单独安装时不存在，跳过）
    if _require_plugin_layout("pyproject 与平台清单的版本号"):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m_proj = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject, _re.M)
        assert m_proj, "pyproject.toml 缺少 version"
        versions["pyproject.toml"] = m_proj.group(1)
        for rel in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
            path = REPO_ROOT / rel
            assert path.is_file(), f"平台声明缺失：{rel}"
            versions[rel] = str(json.loads(path.read_text(encoding="utf-8")).get("version", ""))

    assert len(set(versions.values())) == 1, f"版本号不一致: {versions}"


def check_quality_gate_copy():
    """质检文档口径必须与代码一致：五类致命项齐全，且语言标识写明是提示项。"""
    from src.core.deliverable_lint import FATAL_NOTE_KEYS

    assert len(FATAL_NOTE_KEYS) == 5, f"致命项集合变化，文档需同步：{FATAL_NOTE_KEYS}"

    targets = [("SKILL.md", SKILL_ROOT)]
    if PLUGIN_LAYOUT:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for 中文标签 in ("套话填充", "空壳标题", "分集平铺标题", "行内残缺引用", "分集口吻"):
            assert 中文标签 in readme, f"README.md 质检说明缺少致命项：{中文标签}"
        readme_en = (REPO_ROOT / "README.en.md").read_text(encoding="utf-8").lower()
        for 英文标签 in ("boilerplate", "hollow", "per-episode headings", "inline quote", "episode voice"):
            assert 英文标签 in readme_en, f"README.en.md 质检说明缺少致命项：{英文标签}"
        targets += [("README.md", REPO_ROOT), ("README.en.md", REPO_ROOT)]

    for rel, root in targets:
        text = (root / rel).read_text(encoding="utf-8")
        assert "语言标识" in text or "language tag" in text.lower() or "language identifier" in text.lower(), \
            f"{rel} 未说明围栏语言标识的体检口径"


def check_dispatch_discipline_documented():
    """阶段一派发纪律必须写进文档，不能停留在含糊措辞上（防止回退）。

    阈值：课程总时长 ≤ 60 分钟 → 主 Agent 可串行；超过 → 必须派发。两类角色分工：
    转录角色按块消费（建议 2 个），写作角色按块领集、读逐字稿写长文。回报协议：
    只回报一行、不回传正文。窗口兜底（音频 token 口径）只对转录角色成立。
    """
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for 关键词 in ("60 分钟", "转录角色", "写作角色", "不回传正文", "BVB_AUDIO_TOKENS_PER_SEC"):
        assert 关键词 in skill, f"SKILL.md 缺少阶段一派发纪律关键词：{关键词}"

    if _require_plugin_layout("README 的阶段一派发阈值"):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "60 分钟" in readme and ("派发" in readme), "README.md 未写明阶段一派发阈值"
        readme_en = (REPO_ROOT / "README.en.md").read_text(encoding="utf-8")
        assert "60 minutes" in readme_en or "60-minute" in readme_en, "README.en.md 未写明阶段一派发阈值"


def check_dispatch_payload_shape():
    """派发载荷契约：临时工作区跑一次 queue_tracker，断言字段齐备、台账可写、默认零写入。"""
    import json
    import tempfile

    from src.core import budget
    from src.core.workspace import TaskWorkspace

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="dispatch_probe", base_dir=tmp)
        ws.save_parts([
            {"page": 1, "title": "导学", "duration": 900},
            {"page": 2, "title": "变量", "duration": 1200},
        ])
        block_dir = ws.audio_dir / "_blocks"
        block_dir.mkdir(parents=True, exist_ok=True)
        probe_block = {
            "block_id": 1, "title": "导学与变量", "span": "P01-P02",
            "episodes": [1, 2],
            "units": [{"page": 1, "label": "P01", "split": False},
                      {"page": 2, "label": "P02", "split": False}],
            "audio": "audio/_blocks/探针课_01_导学与变量(P01-P02).m4a",
            "duration_sec": 2100.0, "duration_min": 35.0, "segments": [],
            "single_episode": False, "episode_split": False,
            "oversized": False, "undersized": False,
        }
        (block_dir / "blocks.json").write_text(json.dumps({
            "version": 2, "target_minutes": 50.0, "min_minutes": 40.0, "max_minutes": 60.0,
            "effective_limit_minutes": 75.0, "course_short": "探针课", "noop": False,
            "input_signature": "probe", "blocks": [probe_block],
        }, ensure_ascii=False), encoding="utf-8")
        (block_dir / "探针课_01_导学与变量(P01-P02).m4a").write_bytes(b"x" * 20000)

        tracker = SKILL_ROOT / "scripts" / "queue_tracker.py"

        def _run(*extra, base=None):
            return run_quiet(
                [sys.executable, str(tracker), "--base-dir", str(base or ws.root_dir), *extra],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
            )

        log_path = ws.root_dir / ".dispatch_log.jsonl"
        assert not log_path.exists()

        # 0) --base-dir 既支持「产物根（含工作区）」也支持「工作区目录本身」
        res_root = _run("--summary", base=ws.base_dir)
        assert res_root.returncode == 0, f"--base-dir 指向产物根时失败: {res_root.stdout[-200:]}"
        res_ws = _run("--summary", base=ws.root_dir)
        assert res_ws.returncode == 0, f"--base-dir 指向工作区本身时失败: {res_ws.stdout[-200:]}"
        assert res_root.stdout.split(";")[0] == res_ws.stdout.split(";")[0], "两种 --base-dir 口径结果不一致"

        # 1) 逐字稿未就绪 → 不派发该块（写作角色不该领到没有语料的块），且默认零写入
        res = _run("--next-module", "1", "--json")
        assert res.returncode == 0, f"queue_tracker 退出码 {res.returncode}: {res.stdout[-300:]}"
        assert not log_path.exists(), "未加 --log-dispatch 时不应写台账"
        payload = json.loads(res.stdout)
        for key in ("budget", "next"):
            assert key in payload, f"派发载荷缺少顶层字段：{key}"
        for key in ("suggest_workers", "suggest_batch", "audio_tokens_per_sec", "context_window_tokens",
                    "dispatch_required", "total_audio_min"):
            assert key in payload["budget"], f"budget 缺少字段：{key}"
        assert payload["next"] == [], f"逐字稿未就绪却派发了块: {payload['next']}"

        # 2) 落一份块逐字稿 → 载荷给出块、语料、任务书与目标长文
        transcript = ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("逐字稿正文" * 200, encoding="utf-8")
        res = _run("--next-module", "1", "--json")
        assert res.returncode == 0, f"模块载荷失败: {res.stdout[-300:]}"
        item = json.loads(res.stdout)["next"][0]
        for key in ("block_id", "span", "title", "episodes", "duration_min", "block_audio",
                    "transcript_file", "transcript_bytes", "task_file", "target_article"):
            assert key in item, f"模块载荷缺少字段：{key}"
        assert item["block_id"] == 1 and item["span"] == "P01-P02" and item["episodes"] == [1, 2]
        assert item["transcript_file"].endswith("BLK01_P01-P02_逐字稿.md"), item["transcript_file"]
        assert item["transcript_bytes"] > 0
        assert item["target_article"].endswith("模块01_导学与变量_精读长文.md"), item["target_article"]
        assert item["task_file"].endswith("模块01_导学与变量_TASK.md"), item["task_file"]

        # 2b) 模块长文落盘 → 该块不再派发（≥1000 字节才算完成）
        article = ws.articles_dir / "模块01_导学与变量_精读长文.md"
        article.parent.mkdir(parents=True, exist_ok=True)
        article.write_text("正文" * 400, encoding="utf-8")
        assert json.loads(_run("--next-module", "1", "--json").stdout)["next"] == [], \
            "模块长文已存在却仍派发"

        # 2c) --log-dispatch 写台账，记录的是块号
        article.unlink()
        res2 = _run("--next-module", "1", "--json", "--log-dispatch")
        assert res2.returncode == 0, f"queue_tracker --log-dispatch 失败: {res2.stdout[-300:]}"
        assert log_path.exists(), "--log-dispatch 未写出台账"
        entry = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert entry["suggested_blocks"] == [1], f"台账建议的块与载荷不一致: {entry}"
        assert entry["requested"] == 1 and entry["audio_tokens_per_sec"] == budget.audio_tokens_per_sec()

        # 3) 系数可配置：环境变量覆盖后 est_audio_tokens 同步变化
        env = dict(os.environ, BVB_AUDIO_TOKENS_PER_SEC="100")
        res3 = run_quiet(
            [sys.executable, str(tracker), "--base-dir", str(ws.root_dir), "--summary"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120, env=env,
        )
        assert "AUDIO_TOKENS_PER_SEC=100" in res3.stdout, f"系数覆盖未生效: {res3.stdout.strip()[:200]}"
        assert "SUGGEST_WORKERS=" in res3.stdout and "DISPATCH_REQUIRED=" in res3.stdout

        # 4) 阈值口径：短课程（30 分钟）可串行，长课程必须派发
        assert budget.serial_ok(30 * 60) is True, "30 分钟课程应允许串行"
        assert budget.dispatch_required(30 * 60, episodes=4) is False, "30 分钟 4 集不应强制派发"
        assert budget.dispatch_required(4 * 3600, episodes=40) is True, "4 小时课程必须派发"
        assert budget.suggest_workers(190) == 6 and budget.suggest_workers(2) == 2
        assert budget.suggest_batch([35_000] * 20, 20) == 5, "短集多集应建议打包"
        assert budget.suggest_batch([80_000] * 20, 20) == 1, "长集不应打包"


def check_manifest_paths_portable():
    """清单路径必须可移植：路径字段（含列表型）一律按 to_relative 归一，绝不原样落盘绝对路径。

    注意：临时工作区可能位于**另一个盘符**（TEMP 在 C:、仓库在 D:），此时跨盘 relativize 无法
    产出 `../..` 形式，会退化为绝对路径——因此这里断言的是「落盘值恒等于 to_relative(原值)」，
    而不是「一定不是绝对路径」；另用仓库内路径单独验证相对化后不含盘符。
    """
    import json
    import re as _re
    import tempfile

    from src.core.workspace import TaskWorkspace

    drive_re = _re.compile(r"[A-Za-z]:[\\/]")

    # 相对路径基准必须**恰好是产物根的父目录**——这正是 `output/<task>/...` 在
    # 「容器布局」与「平台安装（无容器）」两种情形下都成立的前提。
    # 不能用 home_root() 当基准：它只是容器的**提示值**，没有容器信号时可能落到与产物根
    # 不同的盘/树，跨盘 relativize 会退化成绝对路径（实测 Windows 平台安装下的真实故障）。
    assert TaskWorkspace.REPO_ROOT == _paths.products_root().parent, \
        f"manifest 相对路径基准应为 products_root().parent，实际 {TaskWorkspace.REPO_ROOT}"

    # 产物根内的路径：相对化后必须是纯相对、无盘符、无反斜杠，且以 output/ 开头。
    # 探针挂在**产物根**下（而非 home_root() 下），才与上面那条基准契约对齐。
    in_repo_rel = TaskWorkspace.to_relative(PRODUCTS_ROOT / "__probe__" / "模块01_甲_精读全书.md")
    assert in_repo_rel == "output/__probe__/模块01_甲_精读全书.md", f"仓库内路径相对化异常: {in_repo_rel}"
    assert not drive_re.search(in_repo_rel) and "\\" not in in_repo_rel

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="portable_probe", base_dir=tmp)
        raw = {
            "textbooks": [str(ws.root_dir / "textbooks" / "模块01_甲_精读全书.md")],
            "notes_files": [str(ws.notes_dir / "模块01_甲_笔记.md")],
            "note_file": str(ws.notes_dir / "模块01_甲_笔记.md"),
            "kernel_file": str(ws.subtitles_dir / "kernels" / "P01_甲_kernel.json"),
            "details": [{"page": 1, "article": str(ws.articles_dir / "P01_甲_精读文章.md")}],
        }
        rel = ws.relativize_obj(raw)
        assert rel["textbooks"] == [TaskWorkspace.to_relative(raw["textbooks"][0])], \
            f"textbooks 未按 to_relative 归一: {rel['textbooks']}"
        assert rel["notes_files"] == [TaskWorkspace.to_relative(raw["notes_files"][0])], \
            f"notes_files 未按 to_relative 归一: {rel['notes_files']}"
        assert rel["note_file"] == TaskWorkspace.to_relative(raw["note_file"]), \
            f"note_file 未按 to_relative 归一: {rel['note_file']}"
        assert rel["kernel_file"] == TaskWorkspace.to_relative(raw["kernel_file"]), \
            f"kernel_file 未按 to_relative 归一: {rel['kernel_file']}"
        assert rel["details"][0]["article"] == TaskWorkspace.to_relative(raw["details"][0]["article"]), \
            "details[].article 未按 to_relative 归一"

        # 反方向：读回时列表型路径字段必须逐项绝对化，程序内部可直接读取
        back = ws.absolutize_obj(rel)
        assert back["textbooks"] == [str(TaskWorkspace.to_absolute(rel["textbooks"][0]))], \
            f"textbooks 未逐项绝对化: {back['textbooks']}"
        assert Path(back["note_file"]).is_absolute(), "note_file 未绝对化"
        assert Path(back["details"][0]["article"]).is_absolute(), "details[].article 未绝对化"

        ws.save_manifest({
            "textbooks": raw["textbooks"],
            "knowledge_blocks_results": [{"block_id": 1, "note_file": raw["note_file"]}],
        })
        text = ws.manifest_file.read_text(encoding="utf-8")
        assert "\\\\" not in text, "manifest.json 落盘了 Windows 反斜杠路径"
        payload = json.loads(text)
        assert payload["textbooks"] == [TaskWorkspace.to_relative(raw["textbooks"][0])], \
            f"textbooks 落盘形态异常: {payload['textbooks']}"
        assert payload["knowledge_blocks_results"][0]["note_file"] == TaskWorkspace.to_relative(raw["note_file"]), \
            f"note_file 落盘形态异常: {payload['knowledge_blocks_results'][0]['note_file']}"


def check_python_syntax_compat():
    """Python 3.10+ 兼容性门禁：全仓源码在 Python 3.10+ 下语法解析无错误。"""
    import ast as _ast

    syntax_hits = []
    targets = []
    for 子目录 in ("src", "scripts"):
        targets.extend(sorted((SKILL_ROOT / 子目录).rglob("*.py")))

    for path in targets:
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SKILL_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")

        try:
            _ast.parse(text, feature_version=(3, 10))
        except SyntaxError as err:
            syntax_hits.append(f"{rel}:{err.lineno}: {err.msg}")

    assert 3 <= len(targets), f"扫描范围异常，只找到 {len(targets)} 个源文件"
    assert not syntax_hits, "存在 Python 3.10 无法解析的语法:\n      " + "\n      ".join(syntax_hits)
    assert sys.version_info >= (3, 10), f"解释器版本低于声明的 3.10: {sys.version.split()[0]}"


def check_no_hardcoded_machine_paths():
    """源码不得硬编码本机盘符绝对路径（AST 取字符串常量；跳过文档字符串里的示例路径）。"""
    import ast
    import re as _re

    drive_re = _re.compile(r"(^|[^\w])[A-Za-z]:[\\/]")

    def _docstring_nodes(tree: ast.AST) -> set:
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    found.add(id(body[0].value))
        return found

    扫描 = []
    # MCP 侧（omni-media 仓库的两个服务）由各自的 selfcheck.py 扫描，本仓库只负责技能侧
    for 子目录 in ("src", "scripts"):
        for path in (SKILL_ROOT / 子目录).rglob("*.py"):
            if path.name == "selfcheck.py":
                continue
            if "__pycache__" in path.parts:
                continue
            扫描.append(path)
    assert 扫描, "未找到任何源码文件，扫描范围异常"

    hits = []
    for path in 扫描:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:
            raise AssertionError(f"源码语法错误，无法扫描: {path}: {err}")
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                if drive_re.search(node.value):
                    hits.append(f"{path.relative_to(SKILL_ROOT).as_posix()}:{node.lineno}: {node.value[:70]}")
    assert not hits, "源码内存在硬编码本机绝对路径:\n      " + "\n      ".join(hits)


def check_regression_fixes():
    """本轮修复项的回归断言（全部在临时工作区内完成，不触碰 output/）。"""
    import json
    import tempfile

    from src.cli import _owner_line
    from src.core.task_cleanup import find_module_note
    from src.core.workspace import TaskWorkspace
    from src.generator.block_synthesizer import BlockSynthesizer
    from src.generator.integrator import ArticleIntegrator

    # 1) merge_parts：局部运行不得丢历史分集，同 page 以新结果为准
    merged = TaskWorkspace.merge_parts(
        [{"page": 1, "title": "旧"}, {"page": 2, "title": "旧"}, {"page": 3, "title": "旧"}],
        [{"page": 2, "title": "新"}],
    )
    assert [m["page"] for m in merged] == [1, 2, 3], f"合并后分集丢失/乱序: {merged}"
    assert merged[1]["title"] == "新", "同 page 未以新结果覆盖"
    assert TaskWorkspace.merge_parts([], [{"page": 5}]) == [{"page": 5}], "空缓存合并不正确"
    assert TaskWorkspace.merge_parts([{"page": 1}], []) == [{"page": 1}], "空增量合并不正确"

    # 2) 离线自愈元数据（owner 为字符串/空）不得让 parse 崩掉
    assert "未知" in _owner_line({"owner": "", "owner_mid": 0}), "owner 为空串时未兜底"
    assert "UP主" in _owner_line({"owner": {"name": "UP主", "mid": 7}}), "正常 owner 渲染异常"
    assert "mid: 9" in _owner_line({"owner_mid": 9}), "仅 owner_mid 时渲染异常"

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="regression_probe", base_dir=tmp)
        ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
        article = ws.articles_dir / "模块01_绪论_精读长文.md"
        article.write_text(
            "# 微型计算机概述" + chr(10)
            + "> 目标：讲清体系结构  " + chr(10)
            + "> 来源：模块长文" + chr(10)
            + chr(10) + "---" + chr(10) + chr(10)
            + "## 1. 体系结构" + chr(10) + chr(10) + "正文内容。" + chr(10)
            + "填充正文，用于越过模块长文的 1000 字节成品门禁。" * 60 + chr(10),
            encoding="utf-8",
        )

        # 3) 笔记复用：成品后缀不是规范名（无 `_笔记`）也必须被认出，不得重复派发
        found = find_module_note(ws, 1)
        assert found is None, "尚无笔记成品时不应命中"
        loose_note = ws.notes_dir / "笔记01_微机系统基础_P01-P17速查.md"
        loose_note.write_text("笔记" * 600, encoding="utf-8")
        assert find_module_note(ws, 1) == loose_note, "非规范后缀的笔记成品未被识别"
        res = BlockSynthesizer.synthesize_block(
            {"block_id": 1, "block_title": "微机系统基础", "episodes": [1],
             "core_theme": "x", "blocks": [1]},
            [article], ws=ws,
        )
        assert res["status"] == "cached", f"已有笔记成品时仍重复派发任务书: {res['status']}"
        assert not (ws.notes_dir / "笔记01_微机系统基础_TASK.md").exists(), "重复派发出了任务书"

        # 4) 教材整编（册=书、章=块）：长文抬头含多行引用时必须剥净、H2 降级，
        #    章标题按块标题渲染、目录按有序列表列出各章，且默认复用 / --force 重编
        block = {"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
                 "duration_min": 30.0, "audio": "audio/_blocks/探针_01_绪论(P01).m4a"}
        integrator = ArticleIntegrator(ws.root_dir)
        volumes = integrator.run(course_title="测试课程", blocks=[block])
        assert len(volumes) == 1, f"单块课程应编成一册: {volumes}"
        out = volumes[0]
        assert out.name == "模块01_测试课程_精读全书.md", out.name
        text = out.read_text(encoding="utf-8")
        out_lines = text.splitlines()
        assert "微型计算机概述" not in text, "长文 H1 未被剥离"
        assert "目标：讲清体系结构" not in text and "来源：模块长文" not in text, "多行抬头未被剥净"
        # 长文标题现要求不写序号；存量带号标题在整编时被幂等剥掉：`## 1. 体系结构` → `### 体系结构`
        assert "### 体系结构" in out_lines, "章内 H2 未降级为 H3（或未剥掉手写序号）"
        assert "## 体系结构" not in out_lines, "章内 H2 仍以 H2 层级残留（与教材章标题同级）"
        assert "### 1. 体系结构" not in out_lines, "整编未剥掉继承自长文的标题序号"
        # 章标题=块标题；目录是有序列表；一律不写「第 N 章」这种手写章号
        assert "## 绪论" in out_lines, "教材章标题未按「块标题」渲染"
        assert "1. 绪论（P01）" in out_lines, "目录未按有序列表列出各章"
        assert "第 1 章" not in text, "教材仍在章标题或目录里写「第 N 章」"
        assert "覆盖范围**：P01 ~ P01" in text, "教材未写明本册覆盖的块范围"
        assert "正文内容。" in text, "正文被误删"

        out.write_text(text + chr(10) + "<!-- MARK -->" + chr(10), encoding="utf-8")
        integrator.run(course_title="测试课程", blocks=[block])
        assert "<!-- MARK -->" in out.read_text(encoding="utf-8"), "默认未复用已存在的教材册"
        integrator.run(course_title="测试课程", blocks=[block], force=True)
        assert "<!-- MARK -->" not in out.read_text(encoding="utf-8"), "--force 未强制重新整编"

        # 5) 任务书回收计数：删除失败与成品未产出必须分开统计
        from src.core.task_cleanup import cleanup_completed_tasks

        (ws.articles_dir / "模块01_绪论_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.articles_dir / "模块02_数制_TASK.md").write_text("t" * 200, encoding="utf-8")
        (ws.articles_dir / "模块02_数制_精读长文.md").write_text("正文" * 400, encoding="utf-8")
        result = cleanup_completed_tasks(ws, keep_per_category=1)
        counts = result["counts"]["articles"]
        assert counts["kept"] == 1 and counts["deleted"] == 1, f"回收计数异常: {counts}"
        assert "failed_delete" in counts and "skipped_pending" in counts, f"回收计数未拆分: {counts}"
        assert counts["failed_delete"] == 0, f"正常删除不应计入失败: {counts}"
        assert list(result["failed_delete"]) == [], "正常删除不应留下失败清单"

        # 6) 清单路径可移植性（与 check_manifest_paths_portable 互补，此处走真实写入链路）
        textbook_path = str(ws.root_dir / "textbooks" / "模块01_绪论_精读全书.md")
        ws.save_manifest({"textbooks": [textbook_path]})
        stored = json.loads(ws.manifest_file.read_text(encoding="utf-8"))["textbooks"][0]
        assert stored == TaskWorkspace.to_relative(textbook_path), \
            f"textbooks 落盘形态与 to_relative 不一致: {stored}"

        # 7) 非视频作品（抖音图文/图集 note）识别与排除
        #    它们没有口播，音频只是图片卡片+BGM，必须识别出来且不派发长文——
        #    否则撰写环节拿不到任何音频事实，只能编造（触红线 2）。
        from src.cli import _persist_parts_cache
        from src.core.ingestion.dyaudio.share_parser import parse_single_aweme
        from src.core.pipeline import KIND_IMAGE_ALBUM, KIND_VIDEO, part_kind

        # 7.1 归一化层：images 非空 → 图文；否则为视频
        assert parse_single_aweme(
            {"aweme_id": "1", "images": [{"url_list": ["a"]}], "video": {"duration": 1000}}
        )["media_kind"] == KIND_IMAGE_ALBUM, "images 非空未判为图文作品"
        assert parse_single_aweme(
            {"aweme_id": "2", "video": {"duration": 1000}}
        )["media_kind"] == KIND_VIDEO, "普通视频被误判为图文作品"
        # 7.2 图文带合成预览视频（play_addr 有值）时仍必须判为图集——
        #     用「有没有视频地址」反推会漏判，这是实现里最容易写错的一处。
        assert parse_single_aweme(
            {"aweme_id": "3", "images": [{"url_list": ["a"]}],
             "video": {"play_addr": {"url_list": ["https://v.example/x"]}}}
        )["media_kind"] == KIND_IMAGE_ALBUM, "图文作品带预览视频时被误判为视频"

        # 7.3 回读兼容：旧 parts.json（B站/YouTube/本地媒体/改动前的抖音）无该字段，
        #     一律按视频处理——这是「不影响既有工作区」的核心保证。
        assert part_kind({"page": 1}) == KIND_VIDEO, "缺 media_kind 时未兜底为视频"
        assert part_kind({"page": 2, "media_kind": None}) == KIND_VIDEO, "media_kind 为 null 时未兜底"
        assert part_kind({"page": 3, "media_kind": ""}) == KIND_VIDEO, "media_kind 为空串时未兜底"
        assert part_kind({"page": 4, "media_kind": KIND_IMAGE_ALBUM}) == KIND_IMAGE_ALBUM

        # 7.4 merge_parts 对 media_kind 的旧值兜底：局部运行的 incoming 若不带该键
        #     （旧调用方 / 旧格式条目），不得把已标记的图文作品静默降级成视频——
        #     否则它会重新进入听音与派发。注意其他字段仍是整体覆盖语义。
        _mk = TaskWorkspace.merge_parts(
            [{"page": 1, "media_kind": KIND_IMAGE_ALBUM, "title": "图文"}],
            [{"page": 1, "title": "新标题"}],
        )
        assert len(_mk) == 1 and _mk[0]["title"] == "新标题", f"同 page 覆盖语义被破坏: {_mk}"
        assert _mk[0]["media_kind"] == KIND_IMAGE_ALBUM, \
            f"merge_parts 把已标记的图文作品降级了: {_mk}"
        # incoming 显式给出该键时以 incoming 为准（允许纠正误判）
        _mk2 = TaskWorkspace.merge_parts(
            [{"page": 1, "media_kind": KIND_IMAGE_ALBUM}],
            [{"page": 1, "media_kind": KIND_VIDEO, "title": "改判"}],
        )
        assert _mk2[0]["media_kind"] == KIND_VIDEO, f"incoming 显式值未生效: {_mk2}"

        # 7.5 跨命令契约：`audio` 单独跑一遍不得把 `pipeline` 写好的 media_kind 冲掉。
        #     这条守的是 _persist_parts_cache 的键白名单——它最容易在新增字段时被漏掉，
        #     一漏就会静默退化（下次 pipeline 认不出图文集）。
        with tempfile.TemporaryDirectory() as tmp2:
            ws2 = TaskWorkspace(task_name="kind_persist", base_dir=tmp2)
            _persist_parts_cache(
                ws2, [{"page": 1, "title": "图文", "media_kind": KIND_IMAGE_ALBUM}]
            )
            _saved = ws2.load_parts()
            assert _saved and _saved[0].get("media_kind") == KIND_IMAGE_ALBUM, \
                f"parts.json 未保留 media_kind（audio 命令会冲掉标记）: {_saved}"


def check_state_sync_block_accounting():
    """对账（`sync`）必须按块跑通：它被 pipeline 包在 try/except 里，崩了只会静默跳过。

    实测事故：`find_module_article` 的第一个参数是 **articles 目录**，而 `state_sync` 传了
    工作区对象——`reconcile_workspace_manifest` 每次抛 TypeError，pipeline 打印「账本对账已跳过」
    了事，账本从此不再回填，而命令看起来一切正常。这条断言就是钉死这个静默失败。
    """
    import json
    import tempfile

    from src.core.state_sync import reconcile_workspace_manifest
    from src.core.workspace import TaskWorkspace

    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="sync_probe", base_dir=tmp)
        ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
        block_dir = ws.audio_dir / "_blocks"
        block_dir.mkdir(parents=True, exist_ok=True)
        (block_dir / "blocks.json").write_text(json.dumps({
            "version": 2, "target_minutes": 50.0, "min_minutes": 40.0, "max_minutes": 60.0,
            "effective_limit_minutes": 75.0, "course_short": "探针", "noop": False,
            "input_signature": "probe",
            "blocks": [{"block_id": 1, "title": "绪论与数制", "span": "P01-P02",
                        "episodes": [1, 2], "duration_min": 46.0,
                        "audio": "audio/_blocks/探针_01_绪论与数制(P01-P02).m4a",
                        "segments": []}],
        }, ensure_ascii=False), encoding="utf-8")

        # 尚未写模块长文：对账必须跑通并如实报「0/1 块完成」，而不是抛异常被吞
        report = reconcile_workspace_manifest(ws, dry_run=True)
        assert report["stage1_unit"] == "module", report
        assert report["blocks_total"] == 1 and report["blocks_done"] == 0, report
        assert report["success"] == 0 and report["pending"] == 2, report
        assert report["pipeline_completed"] is False, report

        # 模块长文落盘后：该块与它覆盖的集号一并记为完成
        (ws.articles_dir / "模块01_绪论与数制_精读长文.md").write_text(
            "正文" * 400, encoding="utf-8")
        report2 = reconcile_workspace_manifest(ws)
        assert report2["blocks_done"] == 1, report2
        assert report2["success"] == 2 and report2["pending"] == 0, report2
        manifest = json.loads(ws.manifest_file.read_text(encoding="utf-8"))
        assert manifest["blocks_done"] == 1 and manifest["stage1_unit"] == "module", manifest
        assert all(d["status"] == "success" for d in manifest["details"]), manifest["details"]


def check_hardening_probes():
    """三路审查（2026-09）发现缺陷的回归防线：Path 入参、畸形清单、占位教材、命名对称、旧成品遮蔽。"""
    import json
    import subprocess
    import tempfile

    from src.core.audio_merger import AudioMerger
    from src.core.workspace import (
        TaskWorkspace,
        find_module_article,
        module_article_stem,
    )
    from src.generator.block_synthesizer import BlockSynthesizer
    from src.generator.integrator import ArticleIntegrator
    from src.generator.topic_planner import SemanticTopicPlanner

    def _write_blocks(ws, blocks, version=2):
        block_dir = ws.audio_dir / "_blocks"
        block_dir.mkdir(parents=True, exist_ok=True)
        (block_dir / "blocks.json").write_text(json.dumps(
            {"version": version, "target_minutes": 50.0, "min_minutes": 40.0, "max_minutes": 60.0,
             "effective_limit_minutes": 75.0, "course_short": "探针", "noop": False,
             "input_signature": "probe", "blocks": blocks}, ensure_ascii=False), encoding="utf-8")

    # 1) 畸形/过期清单一律视为「无清单」：条目缺字段、非 dict、v1 旧版本、空表
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="hardening_probe", base_dir=tmp)
        ws.save_parts([{"page": 1, "title": "绪论"}])
        bad_dir = ws.audio_dir / "_blocks"
        bad_dir.mkdir(parents=True, exist_ok=True)
        for bad in ('{"version": 2, "blocks": [{"block_id": 1}]}',
                    '{"version": 2, "blocks": [null]}',
                    '{"version": 2, "blocks": [{"block_id": 0, "episodes": [1]}]}',
                    '{"version": 1, "blocks": [{"block_id": 1, "episodes": [1]}]}',
                    '{"version": 2, "blocks": []}'):
            (bad_dir / "blocks.json").write_text(bad, encoding="utf-8")
            assert AudioMerger.load_manifest(ws) is None, f"畸形清单未被拒: {bad}"
        assert SemanticTopicPlanner.load_blocks(ws) == [], "畸形清单进了归并链路"
        assert ArticleIntegrator(ws.root_dir).load_blocks() == [], "畸形清单进了整编链路"

    # 2) integrator.run() 的缺省读盘路径：纯 Path 也能读块清单（block_dir 兼容 Path），
    #    且块缺长文时 gate 跳过、不落占位教材（占位会被缓存永久留存）
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="path_probe", base_dir=tmp)
        _write_blocks(ws, [{"block_id": 1, "title": "绪论", "span": "P01",
                            "episodes": [1], "duration_min": 46.0,
                            "audio": "audio/_blocks/探针_01_绪论(P01).m4a"}])
        integrator = ArticleIntegrator(ws.root_dir)
        assert integrator.load_blocks(), "纯路径入参读不到块清单（block_dir 未兼容 Path）"
        results = integrator.run(course_title="探针课", blocks=None)
        assert results == [], f"缺长文却落了教材: {results}"
        assert not list((ws.root_dir / "textbooks").glob("*.md")), "占位教材被落盘"

    # 3) 块号 >99：写（module_article_stem）与读（find_module_article）同一口径
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="id_overflow", base_dir=tmp)
        big = {"block_id": 100, "title": "越界块", "span": "P100-P101", "episodes": [100]}
        stem = module_article_stem(big)
        assert stem.startswith("模块100_"), stem
        target = ws.articles_dir / f"{stem}_精读长文.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("正文" * 400, encoding="utf-8")
        assert find_module_article(ws.articles_dir, big) == target, "块号>99 的长文定位失灵"

    # 4) salvage_notes：非法归并的抢救（重复认领先到先得、未知块忽略、孤块兜底）
    blocks = [{"block_id": 1, "title": "A", "episodes": [1]},
              {"block_id": 2, "title": "B", "episodes": [2]},
              {"block_id": 3, "title": "C", "episodes": [3]}]
    salvaged, diag = SemanticTopicPlanner.salvage_notes(
        [{"note_id": 1, "note_title": "X", "blocks": [1, 2, 2, 99]}], blocks)
    assert [n["blocks"] for n in salvaged] == [[1, 2], [3]], salvaged
    assert [n["episodes"] for n in salvaged] == [[1, 2], [3]], "孤块兜底笔记的集号未推导"

    # 5) 同编号旧成品遮蔽：进入归并态（盘上有 note_plan.json）后宽容命中失效
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="shadow_probe", base_dir=tmp)
        ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
        _write_blocks(ws, [
            {"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
             "duration_min": 46.0, "audio": "audio/a.m4a"},
            {"block_id": 2, "title": "数制", "span": "P02", "episodes": [2],
             "duration_min": 46.0, "audio": "audio/b.m4a"},
        ])
        for bid, title in ((1, "绪论"), (2, "数制")):
            (ws.articles_dir / f"模块{bid:02d}_{title}_精读长文.md").write_text(
                "正文" * 400, encoding="utf-8")
        (ws.root_dir / "note_plan.json").write_text(json.dumps(
            [{"note_id": 1, "note_title": "归并篇", "blocks": [1, 2], "core_theme": "x"}],
            ensure_ascii=False), encoding="utf-8")
        (ws.notes_dir / "笔记01_旧粒度主题_笔记.md").write_text("旧成品" * 400, encoding="utf-8")

        res = BlockSynthesizer.dispatch_notes(ws, [{"page": 1}, {"page": 2}], course_title="探针课")
        task_names = [Path(r["task_file"]).name for r in res["results"]]
        assert task_names == ["笔记01_归并篇_TASK.md"], \
            f"同编号旧成品被误当新归并笔记的缓存: {task_names}"

    # 5b) 教材分册：超体量时按**块边界**均衡切册，块序与章数不变，缺长文的块被 gate
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="volume_probe", base_dir=tmp)
        blocks = [
            {"block_id": 1, "title": "甲", "span": "P01", "episodes": [1],
             "duration_min": 46.0, "audio": "audio/a.m4a"},
            {"block_id": 2, "title": "乙", "span": "P02", "episodes": [2],
             "duration_min": 46.0, "audio": "audio/b.m4a"},
            {"block_id": 3, "title": "丙", "span": "P03", "episodes": [3],
             "duration_min": 46.0, "audio": "audio/c.m4a"},
        ]
        _write_blocks(ws, blocks)
        for bid, title in ((1, "甲"), (2, "乙"), (3, "丙")):
            (ws.articles_dir / f"模块{bid:02d}_{title}_精读长文.md").write_text(
                f"# {title} 长文" + chr(10) + chr(10) + "## 小节" + chr(10) + chr(10)
                + ("正文内容。" * 300) + chr(10), encoding="utf-8")

        integrator = ArticleIntegrator(ws.root_dir)
        # 体量上限压到 1 字节 → 每块一册；三章按块序进书，块序不因分册而乱
        volumes = integrator.run(course_title="探针课", blocks=blocks, size_cap_bytes=1)
        assert len(volumes) == 3, f"超体量未按块分册: {volumes}"
        assert [v.name for v in volumes] == [
            "模块01_探针课（第1册）_精读全书.md",
            "模块02_探针课（第2册）_精读全书.md",
            "模块03_探针课（第3册）_精读全书.md",
        ], [v.name for v in volumes]
        first_text = volumes[0].read_text(encoding="utf-8")
        assert "## 甲" in first_text and "第 1 册 / 共 3 册" in first_text, first_text[:200]

        # 体量上限调大 → 归一册，且旧的多册被清掉（教材是纯派生）
        single = integrator.run(course_title="探针课", blocks=blocks, size_cap_bytes=10_000_000)
        assert len(single) == 1 and single[0].name == "模块01_探针课_精读全书.md", single
        left = sorted(p.name for p in (ws.root_dir / "textbooks").glob("*_精读全书.md"))
        assert left == ["模块01_探针课_精读全书.md"], f"旧册未被清理: {left}"
        body = single[0].read_text(encoding="utf-8")
        assert [body.count(f"## {t}") for t in ("甲", "乙", "丙")] == [1, 1, 1], "章数与块数不符"
        assert body.index("## 甲") < body.index("## 乙") < body.index("## 丙"), "章序与块序不符"

        # 同名章（劈分腿：同一集的上/下两块）必须补范围消歧，过渡句不得变成「A → A」
        dup_blocks = [
            {"block_id": 1, "title": "关系数据库（下）", "span": "P06上", "episodes": [6],
             "duration_min": 43.6, "audio": "audio/d1.m4a"},
            {"block_id": 2, "title": "关系数据库（下）", "span": "P06下", "episodes": [6],
             "duration_min": 43.6, "audio": "audio/d2.m4a"},
        ]
        _write_blocks(ws, dup_blocks)
        for bid, suffix in ((1, "上"), (2, "下")):
            (ws.articles_dir / f"模块{bid:02d}_关系数据库（下）_精读长文.md").write_text(
                "正文内容。" * 300, encoding="utf-8")
        dup_out = integrator.run(course_title="探针课", blocks=dup_blocks, force=True)
        dup_text = dup_out[0].read_text(encoding="utf-8")
        assert "## 关系数据库（下）（P06上）" in dup_text and "## 关系数据库（下）（P06下）" in dup_text,             "同名章未按覆盖范围消歧"
        assert "上一节讲完「关系数据库（下）（P06上）」，下一节接着讲「关系数据库（下）（P06下）」" in dup_text,             "过渡句未使用消歧后的章标题"
        assert "共 1 讲" in dup_text, "讲数未按去重集号统计（劈分腿被重复计数）"

        # 缺长文的块不纳入（gate），也不落占位册
        (ws.articles_dir / "模块03_丙_精读长文.md").unlink()
        kept = integrator.run(course_title="探针课", blocks=blocks, force=True, size_cap_bytes=10_000_000)
        text = kept[0].read_text(encoding="utf-8")
        assert "⚠️" not in text and "## 丙" not in text, "缺长文的块被写进了教材"

    # 6) grounding 冒烟：脚本子进程跑通、exit 0、对「尚无模块长文」如实报告
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="grounding_probe", base_dir=tmp)
        _write_blocks(ws, [{"block_id": 1, "title": "绪论", "span": "P01", "episodes": [1],
                            "duration_min": 46.0, "audio": "audio/a.m4a", "segments": []}])
        res = run_quiet(
            [sys.executable, str(SKILL_ROOT / "scripts" / "article_grounding_check.py"),
             "--dir", str(ws.root_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
        )
        assert res.returncode == 0, res.stdout[-300:] + res.stderr[-200:]
        assert "无模块长文" in res.stdout, res.stdout[-300:]


def check_block_note_merge_contract():
    """块即模块下的归并契约：归并单位是块，判据是块标题 + 模块长文主题，缺归并不终止。

    这一条守的是三件事：

    ① 归并依据必须是**块标题 + 模块长文主题**（不是分集标题，也不是另划一套模块边界）；
    ② 缺归并时按「一块一篇」兜底继续，但**盘上的规划文件一个字节都不改**；
    ③ 粒度变了以后旧粒度的任务书必须作废——否则主 Agent 会照着作废的任务书再派一批
       内容错位的笔记（那比停下来更糟）。
    """
    import json
    import tempfile

    from src.core.workspace import TaskWorkspace
    from src.generator.block_synthesizer import BlockSynthesizer
    from src.generator.topic_planner import SemanticTopicPlanner as P

    # 1) 归并提示词：不许有集数配额与篇数锚点
    for banned in ("1 到 3 集", "极少数大型模块可包含 4 集", "通常包含"):
        assert banned not in P.NOTE_PLAN_PROMPT, f"NOTE_PLAN_PROMPT 仍残留集数配额：{banned}"
    assert "宁可少而厚" in P.NOTE_PLAN_PROMPT, "NOTE_PLAN_PROMPT 必须写明归并宗旨（宁可少而厚）"
    assert "一篇笔记可以装多个块" in P.NOTE_PLAN_PROMPT, \
        "NOTE_PLAN_PROMPT 必须写明一篇笔记可跨多个块"
    # 归并宗旨只讲方向，**不许给篇数锚点**：给了数字，Agent 就会照着凑数或照着了事，
    # 而那正是「1~3 集」那种硬约束的翻版，只是换了个地方出现。
    for 篇数锚点 in ("经验值", "十几篇", "二十来篇", "篇笔记通常", "通常落成"):
        assert 篇数锚点 not in P.NOTE_PLAN_PROMPT, \
            f"NOTE_PLAN_PROMPT 不得出现篇数锚点：{篇数锚点}"

    blocks = [
        {"block_id": 1, "title": "HTML入门、常用标签与表单", "span": "P01-P02",
         "episodes": [1, 2], "duration_min": 46.0},
        {"block_id": 2, "title": "CSS选择器与盒模型", "span": "P03",
         "episodes": [3], "duration_min": 54.0},
    ]

    # 2) 归并依据：块标题 + 模块长文主题（后者是 Agent 读完语料后的后验主题）
    prompt = P.build_note_planning_prompt(
        blocks, course_title="测试课", block_titles={1: "HTML 基础与表单"})
    assert "块 01" in prompt and "HTML入门、常用标签与表单" in prompt, "块清单未按块渲染"
    assert "模块长文主题：《HTML 基础与表单》" in prompt, "归并未采用模块长文主题"
    assert "P01–P02" in prompt and "46 分钟" in prompt, "块清单缺集号或时长"

    # 3) 缺归并 → 「一块一篇」兜底继续，且**不写** note_plan.json
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="merge_task", base_dir=tmp)
        parts = [{"page": 1, "title": "a"}, {"page": 2, "title": "b"}, {"page": 3, "title": "c"}]
        ws.save_parts(parts)
        notes, status, _ = P.resolve_notes(blocks, parts, course_title="课", ws=ws)
        assert status == "unmerged", f"缺归并应兜底继续，实际: {status}"
        assert len(notes) == 2 and notes[0]["episodes"] == [1, 2], notes
        assert (ws.root_dir / "note_plan_TASK.md").exists(), "NOTE_PLAN_TASK 未落盘"
        assert not (ws.root_dir / "note_plan.json").exists(), "兜底归并不得落盘成 note_plan.json"

        # 写了合法归并（一篇跨两个块）后必须被采纳，且集号推导齐全
        (ws.root_dir / "note_plan.json").write_text(json.dumps(
            [{"note_id": 1, "note_title": "合并篇", "blocks": [1, 2], "core_theme": "y"}],
            ensure_ascii=False), encoding="utf-8")
        notes2, status2, _ = P.resolve_notes(blocks, parts, course_title="课", ws=ws)
        assert status2 == "planned" and len(notes2) == 1, "合法归并未被采纳"
        assert notes2[0]["episodes"] == [1, 2, 3], "归并后的集号未推导齐全"

    # 4) 块清单是模块的唯一来源：没有 blocks.json 就没有模块（不得退回按集号硬切）
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="no_blocks", base_dir=tmp)
        assert P.load_blocks(ws) == [], "无块清单时应返回空表"

    # 5) 笔记产物命名：任务书/成品后缀与旧命名 `模块XX` 不再混用
    meta = {"block_id": 4, "block_title": "数据容器体系", "episodes": [28, 46],
            "core_theme": "x", "blocks": [11, 12, 13]}
    assert BlockSynthesizer.get_task_filename(meta) == "笔记04_数据容器体系_TASK.md", \
        BlockSynthesizer.get_task_filename(meta)
    assert BlockSynthesizer.get_note_filename(meta) == "笔记04_数据容器体系_笔记.md"
    assert BlockSynthesizer._blocks_str(meta) == "块 11、块 12、块 13", \
        BlockSynthesizer._blocks_str(meta)
    with tempfile.TemporaryDirectory() as tmp:
        from src.core.task_cleanup import find_module_note
        ws = TaskWorkspace(task_name="note_lookup", base_dir=tmp)
        # 旧命名 `模块XX_…` 已不再兼容：不得被当成现行笔记成品认领
        legacy = ws.notes_dir / "模块04_字面量变量与标识符命名规范_笔记.md"
        legacy.write_text("旧粒度成品" * 300, encoding="utf-8")
        assert find_module_note(ws, 4) is None, "旧命名 `模块XX_*` 不应再被认领"

    # 6) 集号基准以工作区 parts.json 为准（在线全集不得放大工作区范围）
    from src.core.pipeline import resolve_course_title, resolve_scope_parts
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="黑马课程_BV1sHU9BmEne", base_dir=tmp)
        ws.save_parts([{"page": p, "title": f"第{p}讲"} for p in range(9, 88)])
        ws.save_manifest({"title": "工作区标题", "bvid": "BV1sHU9BmEne"})
        info = {"title": "在线标题", "bvid": "BV1sHU9BmEne",
                "parts": [{"page": p, "title": f"在线第{p}讲"} for p in range(1, 186)]}
        scoped = resolve_scope_parts(info, ws)
        assert len(scoped) == 79 and scoped[0]["page"] == 9, \
            f"集号基准被在线全集放大了: {len(scoped)} 集，起始 {scoped[0]['page']}"
        assert resolve_course_title(info, ws) == "工作区标题", "课程标题未以 manifest 为准"

    # 7) 粒度变了以后，旧粒度的任务书必须作废（否则主 Agent 会照旧任务书再派一批错位笔记）
    with tempfile.TemporaryDirectory() as tmp:
        ws = TaskWorkspace(task_name="prune_task", base_dir=tmp)
        ws.save_parts([{"page": 1, "title": "a"}, {"page": 2, "title": "b"}])
        for name in ("笔记01_旧主题A_TASK.md", "笔记02_旧主题B_TASK.md"):
            (ws.notes_dir / name).write_text("旧粒度任务书", encoding="utf-8")
        merged_notes = [{"note_id": 1, "note_title": "合并篇", "blocks": [1, 2], "episodes": [1, 2]}]
        removed = BlockSynthesizer._prune_superseded_tasks(ws, merged_notes)
        assert removed == 2, f"旧粒度任务书未被作废: {removed}"
        assert not list(ws.notes_dir.glob("笔记*_TASK.md")), "作废后不应残留旧任务书"

        # 成品已落盘的任务书不得被作废（那是交付记录，交给 cleanup 回收）
        (ws.notes_dir / "笔记01_旧主题A_TASK.md").write_text("旧粒度任务书", encoding="utf-8")
        (ws.notes_dir / "笔记01_旧主题A_笔记.md").write_text("成品" * 400, encoding="utf-8")
        assert BlockSynthesizer._prune_superseded_tasks(ws, merged_notes) == 0, \
            "成品已落盘的笔记任务书被误删"

    # 8) 笔记与教材都**不做体积切分**：一篇笔记与 note_plan.json 的一条严格一一对应；
    #    教材一册的语料恒为一个块的长文，旧链路「模块过大（>300KB）」的失效模式已不存在。
    #    实测（同模块 23 篇/310KB 对拍）：按体积切笔记会让覆盖不升反降（96.1% → 94.7%）、
    #    体积涨 39%、多出 22 处跨篇重复，并让笔记编号与块映射错位。
    import inspect as _inspect
    for 已移除 in ("enforce_note_size_cap", "enforce_size_cap", "_split_episodes_by_cap"):
        assert not hasattr(P, 已移除), f"模块体积归一已随第一趟规划废除，不得回加：{已移除}"
    for 已移除 in ("enforce_note_size_cap", "enforce_size_cap"):
        assert 已移除 not in _inspect.getsource(BlockSynthesizer), \
            f"笔记派发路径不得再做体积切分：{已移除}"
    integrator_text = (SKILL_ROOT / "src" / "generator" / "integrator.py").read_text(encoding="utf-8")
    for 已移除 in ("group_episodes_by_module", "topic_plan.json", "parts.json"):
        assert 已移除 not in integrator_text, f"教材不再按集号分组、也不再读模块规划：{已移除}"
    # 体量上限仍在，但语义变了：只为**读者**分册（册=书、章=块），与「模块语料体积」无关
    assert "SIZE_CAP_BYTES" in integrator_text,         "分册上限不得被删：一本几 MB 的书在阅读器里翻不动"


def check_heading_number_discipline():
    """标题序号纪律：判定与去号同源、长文与教材不再写号、批量清理脚本可跑且幂等。

    背景（实测踩过两轮）：阅读器（Typora）会**自动**给标题编号，标题里再手写一套
    （笔记的 `## 1. …`、教材的 `## 第 3 章：…` 与继承来的 `## 2.1 …`）就会叠成双号。
    所以口径统一为「标题不写序号」：长文提示词不写号、教材整编幂等去号，
    存量产物由 `scripts/strip_heading_numbers.py` 就清理。
    """
    import importlib.util

    from src.core.deliverable_lint import lint_heading_numbers
    from src.core.heading_numbers import is_numbered_heading, strip_heading_number
    from src.generator.prompt_templates import ARTICLE_LEARNING_PROMPT

    # 1) 长文提示词：不得再要求编号，且必须明说「标题里不要写序号」
    assert "编号加术语" not in ARTICLE_LEARNING_PROMPT, "学习版长文提示词仍在要求标题编号"
    assert "标题里不要写序号" in ARTICLE_LEARNING_PROMPT, "学习版长文提示词缺少「标题不写序号」"
    assert "编号连续" not in ARTICLE_LEARNING_PROMPT, "学习版长文提示词残留「编号连续」"

    # 2) 去号规则：常见形态都剥、内容型数字不剥、幂等、围栏内不动
    for before, after in (
        ("## 1. 列表标签的三大分类", "## 列表标签的三大分类"),
        ("### 2.1 无序列表的语义", "### 无序列表的语义"),
        ("#### 1 这一阶段要拿下的三件事", "#### 这一阶段要拿下的三件事"),
        ("#### 1.3 大小写书写规范", "#### 大小写书写规范"),
        ("### 1 层次化的看问题方法", "### 层次化的看问题方法"),
        ("### 2 类属性", "### 类属性"),
        ("#### 3.7 点击 Install 并等待", "#### 点击 Install 并等待"),
        ("#### 1.1 年月日与时分秒的独立获取", "#### 年月日与时分秒的独立获取"),
        ("## 第 3 章：关系模型", "## 关系模型"),
        ("## 3、工程定位", "## 工程定位"),
    ):
        got = strip_heading_number(before)
        assert got == after, f"去号失败：{before} → {got}"
        assert strip_heading_number(after) == after, f"去号不幂等：{after}"
    for keep in ("## 3 种方案的取舍", "## 1.5 倍速播放", "## 2025 年路线图",
                 "### 1963 年火星火箭：一句 Fortran 循环语句的录入错误",
                 "#### 5.7 与 8.0 版本元数据呈现差异",
                 "#### 0、1 与 NULL 的三值逻辑闭包",
                 "# 篇名", "普通正文行"):
        assert strip_heading_number(keep) == keep, f"内容型数字被误剥：{keep}"
        assert not is_numbered_heading(keep), f"内容型数字被误判为序号：{keep}"
    assert is_numbered_heading("## 1. 带序号的标题"), "带序号的标题未被判定为手写序号"
    assert lint_heading_numbers("```text\n## 1. 围栏内的井号不是标题\n```\n") == [], \
        "围栏内的行被当成标题统计了"

    # 3) 教材整编：章标题与目录都不再写号，且必须调用去号
    src = (SKILL_ROOT / "src" / "generator" / "integrator.py").read_text(encoding="utf-8")
    assert '"## 第 {i} 章：' not in src, "教材仍在章标题里写「第 N 章」"
    assert "- **第 {i} 章**" not in src, "教材目录仍在写「第 N 章」"
    assert "strip_heading_number" in src, "教材整编未对继承来的长文标题去号"

    # 4) 批量清理脚本：真跑一遍（临时文本），去号正确、正文不动、再跑零改动
    script = SKILL_ROOT / "scripts" / "strip_heading_numbers.py"
    assert script.is_file(), "缺少标题去号脚本 scripts/strip_heading_numbers.py"
    spec = importlib.util.spec_from_file_location("_strip_heading_numbers", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    sample = "# 篇名\n\n## 1. 第一节\n\n正文一。\n\n### 2.1 子节\n\n正文二。\n"
    new_text, headings, toc, _changes, _suspects = module.clean_text(sample)
    assert (headings, toc) == (2, 0), f"脚本去号条数不对：headings={headings} toc={toc}"
    assert "## 1." not in new_text and "### 2.1" not in new_text, "脚本未剥掉标题序号"
    assert "正文一。" in new_text and "正文二。" in new_text, "脚本动了正文"
    again_text, headings2, toc2, _c2, _s2 = module.clean_text(new_text)
    assert (headings2, toc2) == (0, 0) and again_text == new_text, "脚本不幂等"
    toc_text, _h3, toc3, _c3, _s3 = module.clean_text("- **第 1 章**：绪论\n")
    assert toc_text == "1. 绪论\n" and toc3 == 1, f"教材目录行归一失败：{toc_text!r}"


def check_fsutil_contract():
    """文件系统健壮性契约：不可访问的条目必须降级为「跳过」，绝不抛异常。

    背景（实测）：产物根里一个 Windows「不受信任的装入点」会让 `Path.is_dir()` 抛
    `OSError(WinError 448)`——`Path.exists()` 会吞掉该错误，`is_dir()` / `stat()` /
    `rglob()` 不会。原实现因此在枚举产物根第一层时就整轮失败，cleanup / sync /
    两个质检脚本与自检里的真实产物门禁**集体失明**。

    这里把 `fsutil` 的降级语义钉死，防止有人把调用点改回裸 `Path.is_dir()` / `stat()`。
    """
    import tempfile

    from src.core import fsutil

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        missing = base / "__definitely_missing__"

        # 1) 不可访问 / 不存在的路径：一律降级，不抛
        assert fsutil.is_dir(missing) is False, "fsutil.is_dir 对不存在的路径应返回 False"
        assert fsutil.file_size(missing) == 0, "fsutil.file_size 对不存在的路径应返回 0"
        assert list(fsutil.iter_child_dirs(missing)) == [], \
            "iter_child_dirs 对不可访问目录应产出空序列而不是抛错"
        assert list(fsutil.iter_files(missing, "*.md")) == [], \
            "iter_files 对不可访问目录应产出空序列而不是抛错"
        assert fsutil.is_reparse_point(missing) is True, \
            "不可访问的路径应按「不可遍历」处理（返回 True）"

        # 2) 正常目录：子目录可枚举、隐藏目录按开关过滤、文件按 pattern 命中
        (base / "ws" / "articles").mkdir(parents=True)
        (base / ".hidden").mkdir()
        (base / "ws" / "articles" / "P01_x_精读文章.md").write_text("x" * 10, encoding="utf-8")
        (base / "ws" / "articles" / "P01_x_TASK.md").write_text("x" * 10, encoding="utf-8")

        assert [p.name for p in fsutil.iter_child_dirs(base, skip_hidden=True)] == ["ws"], \
            "skip_hidden=True 时应只枚举非隐藏子目录"
        assert sorted(p.name for p in fsutil.iter_child_dirs(base)) == [".hidden", "ws"], \
            "默认应枚举全部子目录"
        found = sorted(p.name for p in fsutil.iter_files(base, "*.md"))
        assert found == ["P01_x_TASK.md", "P01_x_精读文章.md"], f"iter_files 命中异常: {found}"

        # 3) 符号链接 / 重解析点（若当前环境允许创建）：必须被跳过，不得被跟随两次
        link = base / "link_to_ws"
        try:
            os.symlink(base / "ws", link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            print("       (当前环境不允许创建符号链接，跳过「链接被跳过」的实测)")
        else:
            assert fsutil.is_reparse_point(link) is True, "符号链接应被识别为重解析点"
            assert link.name not in [p.name for p in fsutil.iter_child_dirs(base)], \
                "iter_child_dirs 必须跳过符号链接（否则同一工作区会被枚举两次）"


def check_bilibili_independent_bv_collection_contract():
    """B 站旧版合集：多种入口统一成一门课，且跨 BV 下载使用各自 BV/CID。

    真实验收场景：``space.bilibili.com/<mid>/lists/<sid>?type=season`` 中的每个条目
    都是独立 BV。旧的 details 接口虽然返回 ``ugc_season``，但 pipeline 只看当前稿件
    的 pages；这次必须把合集 episodes 提升为 P01..PN，并保证 P02 以后不会误用 P01 的
    BV 号取音。
    """
    import tempfile

    from src.core.fetcher import AudioFetcher
    from src.core.ingestion.bilibili import BilibiliProvider
    from src.core.parser import BilibiliParser

    samples = {
        "https://space.bilibili.com/87476569/lists/695667?type=season":
            {"mid": 87476569, "season_id": 695667},
        "https://www.bilibili.com/list/87476569?sid=695667&type=season":
            {"mid": 87476569, "season_id": 695667},
        "https://www.bilibili.com/list/87476569?sid=695667&bvid=BV1RV4y1T7jf&oid=858000462&type=season":
            {"mid": 87476569, "season_id": 695667},
        "https://www.bilibili.com/medialist/play/87476569?business=space_collection&business_id=695667":
            {"mid": 87476569, "season_id": 695667},
        "season:695667":
            {"mid": None, "season_id": 695667},
    }
    for url, expected in samples.items():
        got = BilibiliParser.extract_season_ref(url)
        assert got == expected, f"合集入口识别失败: {url} -> {got}"
        assert BilibiliProvider().match(url), f"BilibiliProvider 未接受合集入口: {url}"
    assert not BilibiliProvider().match("https://example.com/list/1?sid=2"), \
        "非 B 站域名不应因通用 sid 参数被误识别"

    def episode(section: str, idx: int, bvid: str, aid: int, cid: int, title: str, duration: int):
        return {
            "season_id": 695667,
            "section_id": 1,
            "episode_index": idx,
            "aid": aid,
            "cid": cid,
            "bvid": bvid,
            "title": title,
            "duration": duration,
            "page": {"cid": cid, "page": 1, "part": title, "duration": duration},
            "pages": [{"cid": cid, "page": 1, "part": title, "duration": duration}],
            "arc": {"duration": duration},
            "_section": section,
        }

    raw = {
        "title": "P02 临时单集标题",
        "owner": {"name": "测试UP", "mid": 87476569},
        "desc": "",
        "duration": 30,
        "pic": "cover",
        "cid": 2002,
        "aid": 1002,
        "pages": [{"page": 1, "part": "P02 临时单集标题", "cid": 2002, "duration": 30}],
        "ugc_season": {
            "id": 695667,
            "title": "测试独立BV合集",
            "cover": "cover",
            "intro": "测试简介",
            "ep_count": 3,
            "sections": [{
                "title": "第一章",
                "episodes": [
                    episode("第一章", 1, "BV1Le4y1o7v5", 1001, 2001, "第1集", 10),
                    episode("第一章", 2, "BV1Ge411u7d5", 1002, 2002, "第2集", 20),
                    episode("第一章", 3, "BV1RV4y1T7jf", 1003, 2003, "第3集", 30),
                ],
            }],
        },
    }

    original_fetch = BilibiliParser.fetch_video_view.__func__
    original_resolve_seed = BilibiliParser.resolve_season_seed_bvid.__func__

    def fake_fetch(cls, bvid, sessdata=None, wbi_keys_file=None, workspace=None):
        return raw

    def fake_resolve_seed(cls, season_ref, sessdata=None):
        return "BV1Le4y1o7v5"

    try:
        BilibiliParser.fetch_video_view = classmethod(fake_fetch)
        BilibiliParser.resolve_season_seed_bvid = classmethod(fake_resolve_seed)
        info_from_list = BilibiliParser.parse_video(
            "https://www.bilibili.com/list/87476569?sid=695667&bvid=BV1Ge411u7d5&type=season"
        )
        info_from_space = BilibiliParser.parse_video(
            "https://space.bilibili.com/87476569/lists/695667?type=season"
        )
    finally:
        BilibiliParser.fetch_video_view = classmethod(original_fetch)
        BilibiliParser.resolve_season_seed_bvid = classmethod(original_resolve_seed)

    for info in (info_from_list, info_from_space):
        assert info["bvid"] == "BV1Le4y1o7v5", "合集工作区必须以首集 BV 作为稳定入口键"
        assert info["title"] == "测试独立BV合集", f"未提升为课程标题: {info['title']}"
        assert info["has_multi_pages"] is True and len(info["parts"]) == 3
        assert info["duration"] == 60, f"合集总时长未求和: {info['duration']}"
        assert [p["page"] for p in info["parts"]] == [1, 2, 3]
        assert [p["bvid"] for p in info["parts"]] == [
            "BV1Le4y1o7v5", "BV1Ge411u7d5", "BV1RV4y1T7jf",
        ]
        assert [p["cid"] for p in info["parts"]] == [2001, 2002, 2003]

    assert info_from_list["url_page"] == 2, "合集内单集链接应定位到对应 P 序号"
    assert info_from_list["selected_cid"] == 2002
    assert info_from_space["url_page"] is None, "合集首页链接不应被误判为选中某一集"

    seen = {}
    original_stream = AudioFetcher.get_audio_stream_info.__func__
    original_download = AudioFetcher.download_audio.__func__

    def fake_stream(cls, bvid, cid, sessdata=None, prefer_quality="low", wbi_keys_file=None):
        seen["bvid"] = bvid
        seen["cid"] = cid
        return {"best_stream_url": "https://example.invalid/audio.m4s"}

    def fake_download(cls, stream_url, output_filepath, repackage_m4a=True, max_bytes=None, sessdata=None):
        seen["downloaded"] = True
        return str(output_filepath)

    try:
        AudioFetcher.get_audio_stream_info = classmethod(fake_stream)
        AudioFetcher.download_audio = classmethod(fake_download)
        with tempfile.TemporaryDirectory() as tmp:
            BilibiliProvider().fetch_audio(
                {"bvid": "BV1Ge411u7d5", "cid": 2002, "title": "第2集"},
                Path(tmp) / "P02.m4a",
                bvid="BV1Le4y1o7v5",
            )
    finally:
        AudioFetcher.get_audio_stream_info = classmethod(original_stream)
        AudioFetcher.download_audio = classmethod(original_download)

    assert seen == {
        "bvid": "BV1Ge411u7d5",
        "cid": 2002,
        "downloaded": True,
    }, f"跨 BV 下载路由错误: {seen}"


def main():
    print("=" * 62)
    print("video2book 技能自检（技能自包含 + 多宿主声明 + 三域分离）")
    print(f"  技能根  : {SKILL_ROOT}   ← 安装单元（SKILL.md + references/ + src/ + scripts/）")
    if PLUGIN_LAYOUT:
        print(f"  仓库根  : {REPO_ROOT}   ← 插件单元（平台声明 / README / LICENSE）")
    else:
        print("  仓库根  : （技能为单独安装，无插件/仓库布局，仓库级断言已跳过）")
    print(f"  容器根  : {HOME_ROOT}")
    print(f"  产物根  : {PRODUCTS_ROOT}")
    print("=" * 62)
    check("模块导入无 ImportError", check_imports)
    check("CLI 全部子命令 --help 可用", check_cli_help)
    check("三域分离契约（仓库边界/产物在仓库外）", check_repo_separation)
    check("产物根解析（容器标记优先，否则取工作目录）", check_products_root_resolution)
    check("跨仓库不互引（skill ⇎ mcp）", check_no_cross_repo_imports)
    check("复制后的技能目录自包含", check_copied_skill_is_self_contained)
    check("OMNI_STATUS 契约版本兼容", check_contract_parser)
    check("笔记归并契约（块即模块 / 按块号校验 / 无第一趟规划）", check_note_planner_contract)
    check("对账按块跑通（sync 的静默失败防线）", check_state_sync_block_accounting)
    check("审查修复回归（Path/畸形清单/gate/命名对称/遮蔽）", check_hardening_probes)
    check("块级归并契约（块+长文主题为据 / 缺归并不终止 / 旧粒度任务书作废）",
          check_block_note_merge_contract)
    check("文档无悬空小节引用", check_docs_no_dangling_section_refs)
    check("ArticleIntegrator 无硬编码课程数据", check_integrator_no_hardcoded_course)
    check("单集直出长文入口切换", check_transcript_pipeline)
    check("块级转录契约（装箱不劈集/块时长可配/切分幂等）", check_audio_block_contract)
    check("子进程硬超时就位", check_subprocess_timeouts)
    check("MCP 仓库自检（可选段落）", check_mcp_repo_optional)
    check("任务书导出门禁端到端", check_task_file_export_end_to_end)
    check("阶段一门禁不误认任务书", check_stage1_gate_ignores_task_files)
    check("重复分集免字幕复用", check_dedup_reuses_without_subtitles)
    check("SESSDATA 存档安全（脱敏/不入库）", check_sessdata_store_safety)
    check("缓存与凭证路径锚定产物根", check_cache_paths_anchored)
    check("宿主旁路目录与产物不入库", check_host_artifacts_ignored)
    check("交付物渲染兼容约束（Typora）", check_render_compat_rules)
    check("长文提示词风格契约（学习/旧版 + 未确认即终止）", check_article_prompt_types)
    check("模块笔记契约（文章直供/只写结论/版式规范/任务书回收）", check_module_note_contract)
    check("标题序号纪律（长文/教材不写号 + 清理脚本幂等）", check_heading_number_discipline)
    check("交付物机器门禁（告警块/围栏配对）", check_deliverable_lint_gate)
    check("文档无已删除笔记风格残留", check_docs_style_matrix_clean)
    check("交付矩阵长文类型表齐备", check_delivery_matrix_article_types)
    check("版本号三处一致", check_version_consistency)
    check("质检文档口径与门禁一致", check_quality_gate_copy)
    check("清单路径可移植（无绝对路径落盘）", check_manifest_paths_portable)
    check("源码无硬编码本机路径", check_no_hardcoded_machine_paths)
    check("Python 3.10+ 语法兼容", check_python_syntax_compat)
    check("文件系统健壮性契约（坏链接只跳过不崩）", check_fsutil_contract)
    check("B站独立BV合集（多入口归一/跨BV取音）", check_bilibili_independent_bv_collection_contract)
    check("文档层无未证实平台痕迹（不留待确认记录）", check_no_unverified_platform_traces)
    check("阶段一派发纪律已写入文档", check_dispatch_discipline_documented)
    check("派发载荷与台账契约", check_dispatch_payload_shape)
    check("本轮修复项回归", check_regression_fixes)
    check("死代码与验证产物已移除", check_dead_modules_removed)
    check("工作区名推导与找回（BV 号保留/幂等/空壳/歧义）", check_workspace_name_derivation)
    check("技能自包含布局（skills/<name>/ = 安装单元）", check_skill_root_layout)
    check("frontmatter 跨工具安全（仅 name/description/license/metadata）", check_frontmatter_portable)
    check("技能正文无宿主私有工具名（只讲行动语义）", check_no_private_tool_names)
    check("平台声明层自洽（codex/claude/agents 清单 + 入口文件）", check_host_declarations)
    check("宿主工具映射层齐备（未证实平台不编造）", check_host_tools_matrix)
    print("=" * 62)
    if FAILURES:
        print(f"[FAILED] {len(FAILURES)} 项未通过:")
        for name, err in FAILURES:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print("[OK] 全部自检通过")


if __name__ == "__main__":
    main()
