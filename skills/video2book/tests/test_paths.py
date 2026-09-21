# -*- coding: utf-8 -*-
"""产物根与 `--base-dir` 的解析口径（`src/core/paths.py`）。

这几条规则各自对应一类实测故障（原 `scripts/selfcheck.py::check_products_root_resolution`）：

1. 容器标记是从**代码位置**向上找的。技能被软链/复制进平台技能目录后，代码位置仍指向原容器，
   只看标记就会把产物写回原容器，而不是使用者当前干活的目录 → 判定必须带上 cwd；
2. `$BVB_OUTPUT_DIR` 是文档承诺的显式覆盖；**相对路径一律按当前工作目录解析**
   （容器根也一样），否则从容器外调用时会出现「`describe()` 说不在容器内、产物却写回
   容器里」的自相矛盾（第二阶段 A9 修的就是这个）；
3. 清单里的相对路径以**产物根之父目录**为基准；基准跨盘时 `relative_to` / `relpath`
   双双失败并降级成绝对路径，清单失去可移植性；
4. 显式 `--base-dir` 必须压过默认产物根，否则用户指定目录被无声忽略。

末段另收同属 `src/core/` 解析层的 **OMNI_STATUS 线上契约版本兼容**
（原 `check_contract_parser`；本轮只允许改这三个测试文件，故与此同处）。

本模块只用 `monkeypatch` 改环境变量与模块常量、只用 pytest 的 tmp 目录驱动分支：
既不读也不写开发机上真实的 `output/`、`.bvb-home` 或任何容器布局。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core import paths
from src.core.contract import ContractCompatibilityError, parse_compatible_status
from src.core.workspace import TaskWorkspace

# `describe()` 是 `cli.py info` 与自检共同依赖的接口面，键名就是对外契约。
DESCRIBE_KEYS = {
    "code_root",
    "home_root",
    "products_root",
    "home_env",
    "output_env",
    "home_from_env",
    "container_pinned",
    "cwd_inside_container",
    "products_from_env",
    "products_from_cwd",
    "omni_media_repo",
    "omni_media_from_env",
}

_PATH_ENV_VARS = (
    paths.ENV_HOME,
    paths.ENV_OUTPUT_DIR,
    # 听音仓库位置的新旧两个变量名都要清掉：只清新名时，开发机上残留的旧名
    # （$OMNI_MEDIA_MCP_DIR）会让「实际探查」这条分支被绕过，用例静默走错分支。
    paths.ENV_OMNI_MEDIA_DIR,
    paths.LEGACY_ENV_OMNI_MEDIA_DIR,
)


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐条清掉路径类环境变量：开发机上手工设过覆盖时，用例仍须走自己指定的分支。"""
    for name in _PATH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def marker_free_code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把「代码根」挪到一个不可能带 `.bvb-home` 的临时目录。

    真实代码根可能正好位于开发机的容器内（祖先上有标记），那样「无容器信号」这条分支
    永远测不到，用例还会被开发机布局带偏。挪走它，两个显式信号才是干净的。
    """
    code = tmp_path / "install" / "video2book"
    code.mkdir(parents=True)
    monkeypatch.setattr(paths, "CODE_ROOT", code)
    return code


def _mark(container: Path) -> Path:
    container.mkdir(parents=True, exist_ok=True)
    (container / paths.HOME_MARKER).write_text("", encoding="utf-8")
    return container


# ---------------------------------------------------------------------------
# products_root_for：纯函数的三个分支
# ---------------------------------------------------------------------------

def test_products_root_for_uses_cwd_when_no_container_home(tmp_path: Path):
    """没有任何容器信号（新装即用）时，产物根必须是「当前工作目录/output」，而不是猜出来的祖先。"""
    cwd = tmp_path / "work"
    cwd.mkdir()
    assert paths.products_root_for(None, cwd) == cwd.resolve() / paths.DEFAULT_PRODUCTS_DIRNAME


def test_products_root_for_uses_cwd_when_cwd_outside_container(tmp_path: Path):
    """技能软链进平台技能目录后从容器外调用：产物要落在**使用者当前目录**，不是原容器。"""
    home = tmp_path / "container"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert paths.products_root_for(home, outside) == outside.resolve() / paths.DEFAULT_PRODUCTS_DIRNAME


def test_products_root_for_uses_home_when_cwd_is_container_root(tmp_path: Path):
    """cwd 就是容器根时取 `<home>/output`：容器布局的历史锚点不许变。"""
    home = tmp_path / "container"
    assert paths.products_root_for(home, home) == home.resolve() / paths.DEFAULT_PRODUCTS_DIRNAME


def test_products_root_for_uses_home_when_cwd_is_inside_container(tmp_path: Path):
    """cwd 在容器内任意子目录时同样取 `<home>/output`，否则同一容器里跑命令会分裂出多个产物根。"""
    home = tmp_path / "container"
    deep = home / "work" / "deep"
    deep.mkdir(parents=True)
    assert paths.products_root_for(home, deep) == home.resolve() / paths.DEFAULT_PRODUCTS_DIRNAME


# ---------------------------------------------------------------------------
# $BVB_OUTPUT_DIR：显式覆盖
# ---------------------------------------------------------------------------

def test_output_dir_env_absolute_overrides_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`$BVB_OUTPUT_DIR` 是文档写明的显式覆盖：给了它就不许再按容器/cwd 推导。"""
    home = tmp_path / "container"
    work = home / "sub"
    work.mkdir(parents=True)
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(work)
    explicit = tmp_path / "custom-products"
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(explicit))
    assert paths.products_root() == explicit


def test_output_dir_env_relative_resolves_against_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """相对覆盖名一律按**当前工作目录**解析，有容器根时也一样。

    第二阶段 A9：以前有容器根时按容器根解析，于是从容器外调用会自相矛盾——
    `describe()` 报 `cwd_inside_container=False`，产物却写回容器里，用户看到的路径
    与产物实际落点不一致。本模块的总原则是「判定跟着**使用者所在的位置**走」，
    相对覆盖名同理；要把位置钉死就用绝对路径。
    """
    home = tmp_path / "container"
    home.mkdir()
    work = tmp_path / "outside" / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(work)
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, "alt-products")
    assert paths.products_root() == (work / "alt-products").resolve()


def test_describe_agrees_with_products_root_outside_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A9 的核心断言：从容器外调用时，`describe()` 与产物根必须说同一件事。"""
    home = tmp_path / "container"
    home.mkdir()
    work = tmp_path / "outside"
    work.mkdir()
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(work)
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, "alt-products")

    desc = paths.describe()
    assert desc["cwd_inside_container"] is False, desc
    assert desc["products_from_env"] is True, desc
    root = Path(desc["products_root"]).resolve()
    assert root == (work / "alt-products").resolve(), desc
    assert work.resolve() in root.parents, desc
    assert home.resolve() not in root.parents, "产物落回了容器里，与 describe() 的结论矛盾"


def test_output_dir_env_relative_resolves_against_cwd_without_container(
    marker_free_code_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """没有容器根时，相对覆盖名按**当前工作目录**解析（默认语义：产物跟着你干活的地方）。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, "alt-products")
    assert paths.products_root() == (work / "alt-products").resolve()


# ---------------------------------------------------------------------------
# 容器标记 / $BVB_HOME
# ---------------------------------------------------------------------------

def test_container_marker_at_code_root_pins_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """代码根自己放着 `.bvb-home` 时容器根即它本身（容器可整体改名搬迁，标记跟着走）。"""
    container = _mark(tmp_path / "container")
    monkeypatch.setattr(paths, "CODE_ROOT", container)
    assert paths.container_home() == container
    assert paths.is_container_layout() is True


def test_container_marker_in_ancestor_pins_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """标记在上层容器里也要认：包裹技能的层级随安装方式而变，不许写死退几层。"""
    container = tmp_path / "container"
    code = container / "skill" / "skills" / "video2book"
    code.mkdir(parents=True)
    _mark(container)
    monkeypatch.setattr(paths, "CODE_ROOT", code)
    assert paths.container_home() == container


def test_home_env_beats_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`$BVB_HOME` 是比标记更强的显式信号（迁移/临时指向另一容器时必须生效）。"""
    _mark(tmp_path / "marked")
    env_home = tmp_path / "env-home"
    env_home.mkdir()
    monkeypatch.setenv(paths.ENV_HOME, str(env_home))
    assert paths.container_home() == env_home


def test_container_marker_pins_products_root_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """标记存在且 cwd 在容器内 → 产物根 = `<home>/output`（容器布局的历史锚点不变）。"""
    container = _mark(tmp_path / "container")
    monkeypatch.setattr(paths, "CODE_ROOT", container)
    work = container / "skill" / "deep"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    assert paths.products_root() == (container / paths.DEFAULT_PRODUCTS_DIRNAME).resolve()


def test_bvb_home_pins_products_root_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`$BVB_HOME` 指定的容器内跑命令 → 产物根 = `<home>/output`。"""
    home = tmp_path / "container"
    work = home / "deep"
    work.mkdir(parents=True)
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(work)
    assert paths.products_root() == (home / paths.DEFAULT_PRODUCTS_DIRNAME).resolve()


def test_home_is_ignored_when_cwd_is_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """从容器外调用时容器变量不得生效——否则会把产物写回原容器而不是当前干活目录。"""
    home = tmp_path / "container"
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(outside)
    assert paths.products_root() == (outside / paths.DEFAULT_PRODUCTS_DIRNAME).resolve()


def test_no_container_signal_is_not_container_layout(
    marker_free_code_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """全新克隆（无标记、无变量）不得被判成容器布局，否则「容器专属」检查与产物根一起走偏。"""
    assert paths.container_home() is None
    assert paths.is_container_layout() is False


def test_default_products_root_follows_cwd_without_container_signal(
    marker_free_code_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """无容器信号时产物根跟随 cwd（这是默认语义，不是缺陷）。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert paths.products_root() == (work / paths.DEFAULT_PRODUCTS_DIRNAME).resolve()


# ---------------------------------------------------------------------------
# resolve_base_dir / default_base_dir
# ---------------------------------------------------------------------------

def test_explicit_absolute_base_dir_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """显式 `--base-dir` 必须压过产物根：否则用户指定的目录被静默忽略，产物落到别处。"""
    explicit = tmp_path / "my-products"
    elsewhere = tmp_path / "default-products"
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(elsewhere))
    assert paths.resolve_base_dir(str(explicit)) == explicit.resolve()
    assert paths.resolve_base_dir(str(explicit)) != elsewhere


def test_empty_base_dir_falls_back_to_products_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`--base-dir` 缺省或空串都解析到产物根（None / ``""`` / 空白一律同义）。"""
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(tmp_path / "products"))
    expected = paths.products_root()
    assert paths.resolve_base_dir(None) == expected
    assert paths.resolve_base_dir("") == expected
    assert paths.resolve_base_dir("   ") == expected


def test_relative_base_dir_prefers_existing_cwd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """相对 `--base-dir` 优先落到 cwd 下同名目录（拆分前的旧用法不能改变含义）。"""
    elsewhere = tmp_path / "products"
    (elsewhere / "output").mkdir(parents=True)
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(elsewhere))
    work = tmp_path / "work"
    (work / "output").mkdir(parents=True)
    monkeypatch.chdir(work)
    assert paths.resolve_base_dir("output") == (work / "output").resolve()


def test_relative_base_dir_falls_back_to_products_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """cwd 下没有同名目录时，相对 `--base-dir output` 仍须能找到产物根（两级回退）。"""
    elsewhere = tmp_path / "products"
    (elsewhere / "output").mkdir(parents=True)
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(elsewhere))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert paths.resolve_base_dir("output") == (elsewhere / "output").resolve()


def test_relative_base_dir_missing_returns_cwd_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """两处都不存在时返回 cwd 候选（由调用方去建），不得凭空造出一个目录。"""
    elsewhere = tmp_path / "products"
    elsewhere.mkdir()
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(elsewhere))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert paths.resolve_base_dir("output") == (work / "output").resolve()


def test_default_base_dir_matches_products_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`default_base_dir()`（CLI 缺省值）必须与产物根逐字符一致，否则两条解析路径会分裂。"""
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(tmp_path / "products"))
    assert paths.default_base_dir() == str(paths.products_root())


def test_manifest_relative_path_keeps_output_prefix():
    """清单相对路径基准 = 产物根之父目录：任何文件都要写成 `output/<task>/...` 才可移植。

    基准一旦跨盘，`relative_to` / `relpath` 双双失败并降级成绝对路径——这正是历史故障。
    """
    rel = TaskWorkspace.to_relative(
        TaskWorkspace.REPO_ROOT / "output" / "__probe__" / "模块01_甲_精读全书.md"
    )
    assert rel == "output/__probe__/模块01_甲_精读全书.md"


# ---------------------------------------------------------------------------
# describe()：接口面与三个标志位
# ---------------------------------------------------------------------------

def test_describe_exposes_expected_keys(marker_free_code_root: Path):
    """`describe()` 是 `cli.py info` 与自检共用的接口面，键集合就是对外契约。"""
    assert set(paths.describe()) == DESCRIBE_KEYS


def test_describe_reports_env_var_names(marker_free_code_root: Path):
    """展示层必须报出真实变量名，用户据此才知道该设哪个变量。"""
    desc = paths.describe()
    assert desc["home_env"] == "BVB_HOME"
    assert desc["output_env"] == "BVB_OUTPUT_DIR"


def test_describe_flags_pinned_container_inside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """容器标记生效且 cwd 在容器内 → pinned/inside 为真、产物根取 <home>/output。"""
    home = tmp_path / "container"
    work = home / "sub"
    work.mkdir(parents=True)
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(work)
    desc = paths.describe()
    assert desc["container_pinned"] is True
    assert desc["cwd_inside_container"] is True
    assert desc["products_from_env"] is False
    assert desc["products_from_cwd"] is False
    assert desc["products_root"] == str((home / paths.DEFAULT_PRODUCTS_DIRNAME).resolve())


def test_describe_flags_container_outside_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """有容器信号但 cwd 在容器外 → inside 为假、产物跟随 cwd（软链安装后的正常情形）。"""
    home = tmp_path / "container"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.chdir(outside)
    desc = paths.describe()
    assert desc["container_pinned"] is True
    assert desc["cwd_inside_container"] is False
    assert desc["products_from_cwd"] is True
    assert desc["products_root"] == str((outside / paths.DEFAULT_PRODUCTS_DIRNAME).resolve())


def test_describe_flags_products_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`$BVB_OUTPUT_DIR` 覆盖时 products_from_env 为真、products_from_cwd 必为假（互斥）。"""
    monkeypatch.setenv(paths.ENV_OUTPUT_DIR, str(tmp_path / "products"))
    desc = paths.describe()
    assert desc["products_from_env"] is True
    assert desc["products_from_cwd"] is False
    assert desc["products_root"] == str(tmp_path / "products")


def test_describe_flags_products_follow_cwd_without_container(
    marker_free_code_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """无容器信号时 container_pinned 为假、products_from_cwd 为真。"""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    desc = paths.describe()
    assert desc["container_pinned"] is False
    assert desc["cwd_inside_container"] is False
    assert desc["products_from_cwd"] is True
    assert desc["products_root"] == str((work / paths.DEFAULT_PRODUCTS_DIRNAME).resolve())


# ---------------------------------------------------------------------------
# src/core/contract.py：OMNI_STATUS 线上契约的版本兼容
# ---------------------------------------------------------------------------

def test_status_comment_absent_means_no_status():
    """正文里没有状态注释时返回 None：旧版 MCP 的纯文本输出必须继续可用。"""
    assert parse_compatible_status("普通正文，无状态注释") is None


def test_legacy_payload_without_version_is_accepted():
    """缺 `contract_version` 按 legacy v0 接受：线上可能还挂着旧版 MCP，一升级就全拒等于停工。"""
    legacy = '<!-- OMNI_STATUS: {"is_finished": true} -->'
    assert parse_compatible_status(legacy)["is_finished"] is True


def test_current_contract_version_is_accepted():
    """当前主版本的载荷必须原样放行。"""
    current = '<!-- OMNI_STATUS: {"contract_version": 1, "is_finished": false} -->'
    assert parse_compatible_status(current)["contract_version"] == 1


def test_incompatible_major_version_is_rejected():
    """主版本不兼容必须**明确报错**而不是照旧解读：静默按老语义读会写出错的状态。"""
    incompatible = '<!-- OMNI_STATUS: {"contract_version": 2, "is_finished": true} -->'
    with pytest.raises(ContractCompatibilityError) as excinfo:
        parse_compatible_status(incompatible)
    assert "主版本 1" in str(excinfo.value)


def test_nested_payload_is_parsed_intact():
    """嵌套对象要整段解析出来：截断成非法 JSON 会让状态莫名其妙地「解析失败」。"""
    text = '<!-- OMNI_STATUS: {"contract_version": 1, "detail": {"code": 0}} -->'
    assert parse_compatible_status(text)["detail"] == {"code": 0}


@pytest.mark.parametrize("raw", ["true", '"1"', "1.5"])
def test_non_integer_version_is_rejected(raw: str):
    """非整数版本必须拒绝：JSON 的 `true` / `"1"` / `1.5` 都不是主版本号。"""
    with pytest.raises(ContractCompatibilityError) as excinfo:
        parse_compatible_status(f'<!-- OMNI_STATUS: {{"contract_version": {raw}}} -->')
    assert "必须是整数" in str(excinfo.value)


def test_malformed_payload_is_rejected():
    """状态注释本身是坏 JSON 时要报错：静默当成「没有状态」会让上层拿空值继续跑。"""
    with pytest.raises(ContractCompatibilityError):
        parse_compatible_status("<!-- OMNI_STATUS: {oops} -->")
