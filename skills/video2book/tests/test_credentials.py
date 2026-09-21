# -*- coding: utf-8 -*-
"""凭证存档（B 站 SESSDATA / 抖音 Cookie）的安全网。

凭证是这套工具链里唯一「泄露即永久损失」的数据，所以这里钉的是四条底线：
1. 往返一致、空值被拒、脱敏展示不泄露；
2. 显式传入优先，空白显式值视作未传入；
3. 两类凭证**各占一个文件**（否则后写覆盖先写，且安全网会漏一类）；
4. 存档落在**产物根**（不是代码仓库），且被 `.gitignore` 覆盖、未被 git 跟踪。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.core import paths
from src.core.credentials import (
    DEFAULT_DOUYIN_STORE_NAME,
    DEFAULT_STORE_NAME,
    DouyinCookieStore,
    SessdataStore,
    douyin_store_path,
    resolve_douyin_cookie,
    resolve_sessdata,
    store_path,
)

SECRET = "abc123def456ghi789"

# 两类凭证共用的行为契约：任一新增凭证类都必须过这一遍
_STORES = (
    ("SESSDATA", SessdataStore),
    ("抖音 Cookie", DouyinCookieStore),
)


@pytest.mark.parametrize("label,store_cls", _STORES, ids=["sessdata", "douyin"])
def test_store_roundtrip_rejects_blank_and_masks(tmp_path: Path, label, store_cls):
    store = tmp_path / "store.json"

    assert store_cls.load(path=store) is None, f"{label} 无存档时不得凭空返回凭证"
    with pytest.raises(ValueError):
        store_cls.save("   ", path=store)

    store_cls.save(SECRET, path=store)
    assert store_cls.load(path=store) == SECRET, f"{label} 存档往返不一致"
    assert SECRET not in store_cls.mask(SECRET), f"{label} 脱敏展示泄露了完整凭证"

    assert store_cls.clear(path=store) is True
    assert store_cls.clear(path=store) is False, f"{label} 重复清除应返回 False"
    assert store_cls.load(path=store) is None


def test_explicit_sessdata_wins_and_blank_falls_back():
    assert resolve_sessdata(SECRET) == SECRET, "显式传入未优先生效"
    assert resolve_sessdata("   ") == SessdataStore.load(), "空白显式值应回退到本地存档"


def test_explicit_douyin_cookie_wins():
    assert resolve_douyin_cookie(SECRET) == SECRET, "抖音显式传入未优先生效"


def test_stores_are_separate_files():
    """两类凭证必须各占一个文件，否则后写会覆盖先写。"""
    assert store_path() != douyin_store_path(), "两类凭证共用同一存档文件，会互相覆盖"
    assert store_path().name == DEFAULT_STORE_NAME
    assert douyin_store_path().name == DEFAULT_DOUYIN_STORE_NAME


@pytest.mark.parametrize("path_getter", [store_path, douyin_store_path], ids=["sessdata", "douyin"])
def test_credential_paths_anchored_at_products_root(path_getter):
    """存档路径必须锚定产物根，不得随当前所在目录漂移，也不得落回代码仓库。"""
    p = path_getter()
    assert p.is_absolute(), f"凭证存档路径不是绝对路径: {p}"
    assert p.parent == paths.products_root(), f"凭证存档不在产物根: {p}"
    if paths.is_container_layout():
        assert paths.code_root() not in p.parents, f"凭证存档落在了代码仓库内: {p}"


@pytest.mark.parametrize("name", [DEFAULT_STORE_NAME, DEFAULT_DOUYIN_STORE_NAME])
def test_gitignore_covers_every_credential_file(name, repo_root: Path):
    """技能仓库的 `.gitignore` 必须覆盖每一类凭证名；新增一类时漏掉安全网会被这条抓住。

    注意安全网在**技能仓库根**（`成品/skill/.gitignore`），不是技能目录、也不是容器根：
    产物根 `output/` 位于技能仓库之内，因此这条忽略规则才是真正生效的那道。
    """
    ignore = repo_root / ".gitignore"
    if not ignore.exists():  # 安装到平台（无仓库上下文）时没有仓库级 .gitignore
        pytest.skip(f"无仓库级 .gitignore: {ignore}")
    assert name in ignore.read_text(encoding="utf-8"), f"{name} 未被仓库 .gitignore 覆盖（安全网缺失）"


@pytest.mark.skipif(shutil.which("git") is None, reason="未找到 git 命令")
@pytest.mark.parametrize("name", [DEFAULT_STORE_NAME, DEFAULT_DOUYIN_STORE_NAME])
def test_credentials_are_not_tracked_by_git(name, repo_root: Path):
    """凭证存档绝不能进入版本控制。"""
    if not (repo_root / ".git").is_dir():
        pytest.skip(f"{repo_root} 不是 git 仓库")
    tracked = subprocess.run(
        ["git", "ls-files", "--", name],
        cwd=str(repo_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
    )
    assert not tracked.stdout.strip(), f"凭证存档已进入版本控制: {tracked.stdout.strip()}"
