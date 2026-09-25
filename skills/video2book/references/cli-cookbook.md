# CLI 场景手册

本手册是命令用法的单一事实源。所有命令都经：

- `python src/cli.py <子命令>`（推荐）
- `python scripts/run.py <子命令>`（免安装入口）
- `video2book <子命令>`（`pip install -e .` 后）

产物落在产物根，默认是当前工作目录下的 `output/`；用 `--base-dir` 或 `BVB_OUTPUT_DIR` 固定位置。

## 场景一：处理整门课程

```bash
python src/cli.py pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --all --article-type learning
python src/cli.py pipeline "D:\courses\software_engineering" --all --article-type learning
python src/cli.py pipeline "https://www.youtube.com/@freecodecamp" --all --article-type learning
```

`pipeline` 是阶段一唯一入口，内部顺序固定为：元数据 → `parts.json` → `block_plan.json` → 字幕 → 缺字幕块按需音频 → 任务书。B 站字幕完整时不会下载音频；非 B 站来源会为全部块物化音频。

## 场景二：选择分集或区间

```bash
python src/cli.py pipeline "<链接>" --page 3 --article-type learning
python src/cli.py pipeline "<链接>" --range 2-8 --article-type learning
```

`--page` / `--range` 只决定本次处理哪些分集，不会用局部子集重定义已有的完整 `block_plan.json`。

## 场景三：预演与准备阶段一

```bash
# 只解析拓扑，不写 parts、计划、音频或任务书
python src/cli.py pipeline "<链接>" --all --dry-run

# 完成计划、字幕、缺块音频与转录任务书后返回，不派发模块长文任务书
python src/cli.py pipeline "<链接>" --all --audio-only --article-type learning
```

## 场景四：字幕优先与按需音频

字幕阶段由 `pipeline` 内部执行：

1. 从 B 站元数据取得完整分集拓扑；
2. 用 `block_plan.json` 锁定逻辑块；
3. 优先取中文字幕（人工优先，AI 自动字幕兜底）；
4. 覆盖度不足的块进入音频兜底；
5. 只下载缺字幕块涉及的分集音频，并按既有 `units`/`segments` 物化块音频。

字幕 CDN 返回残缺正文时，字幕服务按 `BVB_SUBTITLE_ATTEMPTS`（默认 4 轮）退避重试；确实没有中文字幕的块不会被伪造或静默跳过，而是进入音频兜底。

## 场景五：取派发载荷

```bash
# 写作侧：逐字稿已就绪且模块长文缺失
python scripts/queue_tracker.py --next-module 5 --json --log-dispatch

# 转录侧：逐字稿缺失且物理音频已就绪
python scripts/queue_tracker.py --next-transcribe --json

# 笔记侧
python scripts/queue_tracker.py --next-note 5 --json

# 单行进度
python scripts/queue_tracker.py --summary
```

载荷中的 `dispatch_prompt` 必须原样透传。缺物理音频的块不会进入转录载荷；应由 `pipeline` 先完成按需物化。

## 场景六：笔记与教材

```bash
python src/cli.py cluster-notes "<链接或工作区>"
python src/cli.py cluster-articles "<链接或工作区>"
```

`cluster-notes` 读 `block_plan.json` 与模块长文，导出 `note_plan_TASK.md`；`cluster-articles` 按块序整编教材。两者收尾都会执行任务书回收与账本对账。

## 场景七：质量门禁

```bash
# 阶段一：模块长文是否基于本块逐字稿
python src/cli.py check --stage1 --strict

# 交付前：笔记成色 + 渲染合规
python src/cli.py check --deliver --strict
python src/cli.py check --deliver --strict --require-structure
python src/cli.py check --deliver --strict --require-lang
python src/cli.py check --deliver --strict --require-no-numbering

# 清理存量标题手写序号
python src/cli.py check --fix-numbering --dry-run
python src/cli.py check --fix-numbering
```

阶段一使用双层实体覆盖率：英文标识符与多位数字、中文技术术语骨架；任一层达到 `--min-coverage` 即放行。中文层固定 3 次门槛，`--min-freq` 只调英文层。

## 场景八：收尾

```bash
python src/cli.py cleanup --dry-run
python src/cli.py cleanup
python src/cli.py sync
```

`pipeline`、`cluster-notes`、`cluster-articles` 会自动收尾；独立命令用于复算或补做。

## 子命令全表

| 子命令 | 用途 |
| :--- | :--- |
| `pipeline` | 阶段一唯一入口：元数据、BlockPlan、字幕、按需音频与任务书 |
| `cluster-notes` | 块 → 笔记归并，导出笔记任务书 |
| `cluster-articles` | 按块序把模块长文整编成册 |
| `check` | 质量门禁：阶段一放行、交付体检、标题去号 |
| `cleanup` | 回收已完成任务书，每类保留 1 份范本 |
| `sync` | 以磁盘产物回填 `manifest.json` |
| `info` | 环境、工具链与凭证状态 |
| `login` | 持久化 SESSDATA / 抖音 Cookie |
| `logout` | 清除已保存凭证 |

## 完整参数表

| 入口 | 参数 | 用途 |
| :--- | :--- | :--- |
| `pipeline` | `--all` `--range X-Y` `--page N` `--quality <档>` `--block-minutes N` `--force` `--article-type <风格>` `--dry-run` `--audio-only` `--task NAME` `--base-dir DIR` | 阶段一主入口；`--force` 只重取物理音频/逐字稿/任务书，不改变已有计划 |
| `cluster-notes` | `--force` `--block-id N` `--start-block N` `--end-block N` | 笔记归并派发；后三者按笔记序号筛选 |
| `cluster-articles` | `--force` | 默认复用已有教材，`--force` 按最新计划重编 |
| `check` | `--stage1` `--deliver` `--fix-numbering` `--strict` `--dir` `--task` `--base-dir` `--json` `--min-freq N` `--min-coverage F` `--max-truncated N` `--require-structure` `--require-lang` `--require-no-numbering` `--only` `--dry-run` `--max-samples N` `--hash-nonheading` | 质量门禁与存量标题清理 |
| `cleanup` | `--keep N` `--dry-run` `--task 关键字` | 每类保留 N 份任务书范本 |
| `sync` | `--dry-run` `--task 关键字` | 按磁盘对账回填 manifest |
| `queue_tracker.py` | `--next-transcribe [N]` `--next-module N` `--next-note N` `--summary` `--json` `--dir PATH` `--pattern/--task 关键字` `--base-dir DIR` `--log-dispatch` | 派发载荷与阶段进度 |

## 退出码

- `0`：正常结束
- `1`：通用错误、工作区缺失或参数非法
- `2`：元数据、块计划或音频准备失败
- `3`：任务书导出或阶段编排失败
- `4`：未确认长文提示词风格

## 环境与凭证

依赖与听音通道见 `references/runtime.md`；平台工具名映射见 `references/host-tools/`。