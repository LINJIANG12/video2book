"""video2book 技能**结构门禁**自检。

Run: python scripts/selfcheck.py

范围（重要）：本脚本只守「结构与一致性」——安装单元布局、跨仓库边界、文档与实现同源、
版本号一致、静态 lint（子进程超时 / 硬编码本机路径 / Python 3.10 语法）、以及各类
「不许回流」的防膨胀断言。

**行为契约不在这里**：装箱与切分、门禁口径、笔记与教材整编、凭证存档、提示词风格、
派发载荷、路径解析等，全部由 `tests/`（pytest，见 pyproject.toml 的 pytest 配置）覆盖。
把行为断言写回本文件是倒退——pytest 有 fixture 与参数化，这个脚本没有。
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
# 听音服务的位置**不在本文件里猜**：统一走 paths.py 的解析
# （含 $OMNI_MEDIA_DIR / 迁移前旧名 $OMNI_MEDIA_MCP_DIR 覆盖、实际探查优先与旧布局兜底）。
#
# 0.4.0 起两条听音通道（read_audio / read_media）合并为**同一个仓库里的同一个包**，
# 由 `--mode native|ext` 区分，所以这里只有一个路径，不再有「两个子目录」的概念。
OMNI_REPO = _paths.omni_media_repo()

FAILURES = []

# git 可用性：有断言要靠 `git ls-files` 校验「产物/凭证未入库」。
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
    import importlib
    for mod in (
        "src.cli",
        "src.prompts",
        "src.core.console",
        "src.core.fsutil",
        "src.core.paths",
        "src.pipeline",
        "src.core.workspace",
        "src.core.ingestion",
        "src.generator.topic_planner",
        "src.generator.integrator",
        "src.generator.block_synthesizer",
    ):
        importlib.import_module(mod)


# 收敛后的 CLI 唯一入口面（`check_cli_help` / `check_cli_surface_consolidated` / 文档全表共用）。
# `parse` / `audio` / `dedup` / `split-transcript` 已分别并入 pipeline 与 check，不许回加。
CLI_SUBCOMMANDS = (
    "pipeline", "merge-audio", "cluster-notes", "cluster-articles",
    "check", "cleanup", "sync", "login", "logout", "info",
)


def check_cli_help():
    # 一集一篇的 `transcribe` 入口已随旧链路整体移除，不许回加
    for sub in CLI_SUBCOMMANDS:
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
    # 解析 / 取音两个轻量入口已收敛为 pipeline 的开关（取代原 `parse` / `audio` 子命令）
    for 开关 in ("--dry-run", "--audio-only"):
        assert 开关 in res.stdout, f"pipeline 缺少 {开关}（解析 / 取音入口已收敛进来）"
    for 已移除 in ("--no-merge", "--chunk-minutes"):
        assert 已移除 not in res.stdout, f"pipeline 不该再有 {已移除}（逐集听音链路已整体移除）"


def check_cli_surface_consolidated():
    """入口面收敛与防膨胀：SKILL.md 行数上限 + 唯一执行路径 + 子命令集合与文档全表一致。

    这条守的是「Agent 读取到执行之间的路径不能再次变长」：契约层（`SKILL.md`）超过 200 行、
    子命令与文档脱节、或主流程里冒出并列的备选做法，都会在这里转红。
    """
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    line_count = len(skill.splitlines())
    assert line_count <= 200, f"SKILL.md 行数 {line_count} 超过 200 行（契约层必须保持精简）"

    # 唯一执行路径：只声明一次，且不出现并列主路径表述
    assert skill.count("唯一执行路径") == 1, "SKILL.md 应只声明一条唯一执行路径"
    for 反模式 in ("另一条路径", "备选路径", "可选路径", "两种做法", "也可以只"):
        assert 反模式 not in skill, f"SKILL.md 出现并列主路径表述「{反模式}」，应只保留一条"

    # CLI 全表必须与实现同源：恰好列出 CLI_SUBCOMMANDS，且已收敛入口不得回流
    cookbook = (SKILL_ROOT / "references" / "cli-cookbook.md").read_text(encoding="utf-8")
    table_start = cookbook.find("## 子命令全表")
    assert table_start >= 0, "cli-cookbook.md 缺少「子命令全表」"
    table = cookbook[table_start:]
    for sub in CLI_SUBCOMMANDS:
        assert f"`{sub}`" in table, f"cli-cookbook 子命令全表缺少 `{sub}`"
    for 已收敛 in ("`parse`", "`audio`", "`dedup`", "`split-transcript`"):
        assert 已收敛 not in table, f"cli-cookbook 子命令全表仍列出已收敛入口 {已收敛}"


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
                                     (_paths.DEFAULT_OMNI_REPO_DIRNAME, OMNI_REPO)):
            if not repo_root.exists():
                continue
            try:
                PRODUCTS_ROOT.relative_to(repo_root)
            except ValueError:
                continue
            raise AssertionError(f"产物根 {PRODUCTS_ROOT} 位于 {repo_name} 仓库工作树内")

    if container:
        if OMNI_REPO.is_dir():
            assert (OMNI_REPO / ".git").is_dir(), \
                f"{_paths.DEFAULT_OMNI_REPO_DIRNAME}/ 应是独立 git 仓库（缺 .git）"
        assert not (REPO_ROOT / "omni-media-mcp").exists(), "仓库根不应再残留 omni-media-mcp/"

    # 产物根必须可用：不存在就按工具的默认语义建出来（任何命令首次写入也会建它），
    # 这样全新克隆下自检不必依赖「恰好已经跑过一次 pipeline」。
    if not PRODUCTS_ROOT.is_dir():
        try:
            PRODUCTS_ROOT.mkdir(parents=True, exist_ok=True)
            print(f"[*] 产物根此前不存在，已按默认语义自动创建: {PRODUCTS_ROOT}")
        except OSError as err:
            raise AssertionError(f"产物根不存在且无法创建: {PRODUCTS_ROOT}（{err}）")


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

    # 反向扫描听音仓库：不得 import 技能包 `src`（三域分离：技能是听音服务的消费者，不是依赖方）。
    #
    # 「两个 MCP 运行代码不得互相 import」这条随统一包一并作废——过去两者分属两个包、
    # 天然隔离，现在它们本来就是同一个包里的模块，再要求互不 import 等于禁止内聚。
    if OMNI_REPO.is_dir():
        bad = []
        # 只扫**源码目录**，不用 rglob 扫全仓：仓库根可能临时出现各种非源码目录
        # （构建产物、抓取下来的对照文本……），它们里面的 *.py 未必是 Python，
        # 用 rglob 会让「语法解析失败」变成一个与本次检查毫无关系的假报警。
        for base in ("omni_media", "tests"):
            root = OMNI_REPO / base
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                rel = path.relative_to(OMNI_REPO).as_posix()
                # AST 只看 import 语句；再补一层文本扫描，防动态导入绕过
                if "src" in _imported_modules(path) or _re.search(r"^\s*(?:import|from)\s+src\b", text, _re.M):
                    bad.append(rel)
        assert not bad, f"听音仓库不得 import 技能包 src: {bad}"


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
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
        work = root / "work"
        work.mkdir()
        # 剥掉产物根/容器根覆盖：本检查断言的是「无关 cwd 下的**默认**产物语义」。
        # 带着 `BVB_OUTPUT_DIR` 跑自检（在临时产物根下跑自检的常规做法）时，子进程会**正确地**
        # 采用那个覆盖值，而 `work/output` 只在默认语义下才成立——不剥就会把一次正常的
        # 覆盖判成「复制后不自包含」，一条看环境变色的断言。
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in (_paths.ENV_OUTPUT_DIR, _paths.ENV_HOME)
        }
        result = run_quiet(
            [sys.executable, str(copied / "src" / "cli.py"), "info"],
            cwd=str(work),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (result.stdout or "")[-500:]
        assert "Python 运行环境" in (result.stdout or "")
        assert str(work / "output") in (result.stdout or "")
    return "复制后的技能目录可从任意 cwd 独立运行"


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
    """可选段落：存在听音仓库时调用它自己的自检（技能侧不依赖它）。

    听音服务的全部不变量（工具契约、limits、适配器、废弃链路）由 `omni-media/` 自己的
    `selfcheck.py` 负责，避免两处断言各自漂移；找不到该仓库即跳过，不视为失败。
    """
    import subprocess as _sp

    # 探查契约：`omni_media_repo()` 必须**实际存在于候选父目录里**，而不是拿 home_root() 拼字符串
    # （平台安装下 home_root() 只是提示值，拼出来的路径往往根本不存在）。
    bases = _paths.omni_candidate_bases()
    assert bases, "听音仓库候选父目录为空"
    assert bases[0] == PRODUCTS_ROOT.parent, \
        f"候选首位应为产物根的父目录（默认布局下与 omni-media/ 平级）: {bases[0]}"

    _repo = _paths.omni_media_repo()
    if _repo.is_dir() and not (
        os.environ.get(_paths.ENV_OMNI_MEDIA_DIR, "").strip()
        or os.environ.get(_paths.LEGACY_ENV_OMNI_MEDIA_DIR, "").strip()
    ):
        assert _repo.name == _paths.DEFAULT_OMNI_REPO_DIRNAME, \
            f"omni_media_repo() 命中的路径形态异常（应为 <base>/omni-media）: {_repo}"

    # 旧的两子包布局必须**已消失**：留着就等于两套实现各自漂移（这正是本次收敛要消除的问题）。
    for 旧子目录 in ("mcp", "mcp-ext"):
        assert not (_repo / 旧子目录).is_dir(), \
            f"omni-media/{旧子目录} 已随统一包删除，不许回流（两条通道由 --mode 区分）"

    entry = _repo / "selfcheck.py"
    if not entry.is_file():
        print(f"       (未发现 {entry}，跳过)")
        return
    res = run_quiet(
        [sys.executable, str(entry)],
        cwd=str(_repo), stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, timeout=300,
    )
    tail = "\n".join((res.stdout or "").strip().splitlines()[-4:])
    assert res.returncode == 0, f"{entry} 未通过（exit {res.returncode}）:\n{tail}"
    print(f"       (已调用 {entry})")


def check_dead_modules_removed():
    # 技能目录内（随 skill 移动）
    for rel in (
        "src/core/http_client.py",
        "src/core/kernel_extractor.py",  # 逐集知识元特性随逐集链路一并移除
        "src/generator/cleaner.py",
        "src/generator/classifier.py",
        "src/generator/doc_builder.py",
        "scripts/validate_skill.py",
        # 入口收敛：与 `cleanup` / `check` 重复或已被合并的独立脚本，不许回流
        "scripts/cleanup_tasks.py",
        "scripts/article_grounding_check.py",
        "scripts/note_quality_check.py",
        "scripts/render_compat_check.py",
        "scripts/strip_heading_numbers.py",
    ):
        assert not (SKILL_ROOT / rel).exists(), f"{rel} 应已删除"

    # 物理构建与安装元数据防呆断言：
    # build/ 为物理编译构建目录，不得滞留；
    # video2book.egg-info/ 可由 `pip install -e .` 本地/CI生成，但绝不得纳入 git 仓库追踪。
    assert not (REPO_ROOT / "build").exists(), "仓库根不得滞留 build/ 构建产物目录"
    if _HAS_GIT:
        tracked_egg = run_quiet(
            ["git", "ls-files", "--", "skills/video2book/video2book.egg-info", "video2book.egg-info"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        assert not (tracked_egg.stdout or "").strip(), "video2book.egg-info/ 不得纳入 git 仓库追踪"

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

    # 把听音仓库一并纳入校验（它也是一个独立 git 仓库）
    repos = [REPO_ROOT] + [p for p in (OMNI_REPO,) if (p / ".git").is_dir()]

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


# 技能正文里**不得出现**的宿主私有工具名（技能只讲行动语义：「读文件」「写盘」）。
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
    from src.prompts import ARTICLE_PROMPT_TYPES, IMPLEMENTED_ARTICLE_TYPES

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
    assert "next_start_time" in skill, "SKILL.md 续读参数必须使用 next_start_time（防死循环）"

    if _require_plugin_layout("README 的阶段一派发阈值"):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "60 分钟" in readme and ("派发" in readme), "README.md 未写明阶段一派发阈值"
        readme_en = (REPO_ROOT / "README.en.md").read_text(encoding="utf-8")
        assert "60 minutes" in readme_en or "60-minute" in readme_en, "README.en.md 未写明阶段一派发阈值"


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


def check_layering():
    """分层约束：依赖只能自上而下，且共享内容必须是叶子。

    真实边界（v3.0.0 确立）：

        cli.py / pipeline.py  →  generator/  →  core/
                              ↘             ↘
                                prompts.py（叶子：只准 import 标准库）

    - `core/` 是最低层：不得 import `generator` / `pipeline` / `cli`。
      过去 `core/pipeline.py` 调 `BlockSynthesizer.dispatch_notes`、`core/taskbook.py`
      读 `generator.prompt_templates`，两个包之间存在真实的双向依赖环；
      已把编排器上移为 `src/pipeline.py`、把共享提示词下沉为 `src/prompts.py`。
    - `generator/` 不得 import 上层编排（`pipeline` / `cli`）。
    - `prompts.py` 被 core 与 generator 共同依赖，它一旦反向依赖任何一层，环就回来了。
    """
    import ast

    # 每层各自禁止依赖的上层
    禁止 = {
        "core": ("src.generator", "src.pipeline", "src.cli"),
        "generator": ("src.pipeline", "src.cli"),
    }

    def 顶层导入(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    out.append((node.module, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.append((alias.name, node.lineno))
        return out

    hits = []
    for layer, forbidden in 禁止.items():
        layer_dir = SKILL_ROOT / "src" / layer
        assert layer_dir.is_dir(), f"分层目录缺失: src/{layer}"
        for path in layer_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(SKILL_ROOT).as_posix()
            for mod, lineno in 顶层导入(path):
                for bad in forbidden:
                    if mod == bad or mod.startswith(bad + "."):
                        hits.append(f"{rel}:{lineno} → {mod}（{layer} 不得依赖 {bad}）")
    assert not hits, "分层被破坏（依赖只能自上而下）:\n      " + "\n      ".join(hits)

    # 共享提示词必须是叶子
    prompts_path = SKILL_ROOT / "src" / "prompts.py"
    assert prompts_path.is_file(), "缺少共享提示词叶子模块 src/prompts.py"
    leaf_hits = [
        f"src/prompts.py:{lineno} → {mod}"
        for mod, lineno in 顶层导入(prompts_path)
        if mod == "src" or mod.startswith("src.")
    ]
    assert not leaf_hits, \
        "prompts.py 必须只依赖标准库（它是 core 与 generator 的共享叶子）:\n      " + \
        "\n      ".join(leaf_hits)

    # `src/` 顶层模块只能用绝对导入。
    # 顶层模块里的 `from .x import y` 解析成 `src.x`，必然 ImportError；而这类失败常被
    # 就地 try/except 吞掉，只在整条流水线跑起来时才表现为「某步骤被静默跳过」——
    # v3.0.0 把 pipeline.py 从 core/ 上移到 src/ 时就踩过一次（任务书回收与账本对账双双失效，
    # 596 个 pytest 用例全绿也没发现，靠 golden 快照才抓出来）。故在此永久钉死。
    rel_hits = []
    for path in sorted((SKILL_ROOT / "src").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                rel = path.relative_to(SKILL_ROOT).as_posix()
                rel_hits.append(f"{rel}:{node.lineno} → from {'.' * node.level}{node.module or ''} import ...")
    assert not rel_hits, \
        "src/ 顶层模块不得使用相对导入（会解析成 src.* 而 ImportError）:\n      " + \
        "\n      ".join(rel_hits)


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
    print("【结构门禁】——行为契约见 tests/（pytest）")
    check("模块导入无 ImportError", check_imports)
    check("CLI 全部子命令 --help 可用", check_cli_help)
    check("入口面收敛（SKILL.md ≤200 行 + 唯一路径 + 子命令与文档同源）", check_cli_surface_consolidated)
    check("三域分离契约（仓库边界/产物在仓库外）", check_repo_separation)
    check("跨仓库不互引（skill ⇎ omni-media）", check_no_cross_repo_imports)
    check("分层边界（core ⇎ generator 单向；prompts 为叶子）", check_layering)
    check("复制后的技能目录自包含", check_copied_skill_is_self_contained)
    check("文档无悬空小节引用", check_docs_no_dangling_section_refs)
    check("子进程硬超时就位", check_subprocess_timeouts)
    check("听音仓库自检（可选段落）", check_mcp_repo_optional)
    check("死代码与验证产物已移除", check_dead_modules_removed)
    check("宿主旁路目录与产物不入库", check_host_artifacts_ignored)
    check("技能自包含布局（skills/<name>/ = 安装单元）", check_skill_root_layout)
    check("frontmatter 跨工具安全（仅 name/description/license/metadata）", check_frontmatter_portable)
    check("技能正文无宿主私有工具名（只讲行动语义）", check_no_private_tool_names)
    check("平台声明层自洽（codex/claude/agents 清单 + 入口文件）", check_host_declarations)
    check("宿主工具映射层齐备（未证实平台不编造）", check_host_tools_matrix)
    check("文档层无未证实平台痕迹（不留待确认记录）", check_no_unverified_platform_traces)
    check("文档无已删除笔记风格残留", check_docs_style_matrix_clean)
    check("交付矩阵长文类型表齐备", check_delivery_matrix_article_types)
    check("版本号三处一致", check_version_consistency)
    check("质检文档口径与门禁一致", check_quality_gate_copy)
    check("阶段一派发纪律已写入文档", check_dispatch_discipline_documented)
    check("源码无硬编码本机路径", check_no_hardcoded_machine_paths)
    check("Python 3.10+ 语法兼容", check_python_syntax_compat)
    print("=" * 62)
    if FAILURES:
        print(f"[FAILED] {len(FAILURES)} 项未通过:")
        for name, err in FAILURES:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print("[OK] 全部自检通过")


if __name__ == "__main__":
    main()
