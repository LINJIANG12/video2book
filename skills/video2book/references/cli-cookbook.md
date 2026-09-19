# CLI 场景手册

本手册收录按任务目标划分的完整命令清单。README 只给出最常见的四条示例，其余场景在此展开。

## 运行约定

- **工作目录 = 技能目录**（`SKILL.md` 所在目录，即 `skills/video2book/`）。下文所有 `python src/cli.py …` / `python scripts/…` 均以此为当前目录；
- 若已执行 `pip install -e .`，可直接使用 `video2book` 命令，等价于 `python src/cli.py`；
- 产物一律落在**产物根**，与代码目录分离；下文示例中的 `output/<task>/…` 均**相对产物根**；
  **产物根默认是「你跑命令时的工作目录」下的 `output/`**（在容器内工作时为 `<容器根>/output`）；
- 想固定位置：`--base-dir <路径>`，或环境变量 `BVB_OUTPUT_DIR`（产物根）/ `BVB_HOME`（容器根）。

---

## 场景一：处理整门课程流水线

```bash
# 处理整门 B 站网课合集（--article-type 必填，不传即 exit 4）
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --all --article-type learning

# 处理「每个分集都是独立 BV」的 B 站旧版合集（任一入口都可，自动展开整季）
video2book pipeline "https://space.bilibili.com/87476569/lists/695667?type=season" --all --article-type learning
video2book pipeline "https://www.bilibili.com/list/87476569?sid=695667&type=season" --all --article-type learning
video2book pipeline "https://www.bilibili.com/video/BV1RV4y1T7jf" --all --article-type learning

# 处理本地整套视频课程目录
video2book pipeline "D:\courses\software_engineering\" --all --article-type learning

# 处理 YouTube 单视频或播放列表/频道课程
video2book pipeline "https://www.youtube.com/watch?v=kqtD5dpn9C8" --article-type learning
video2book pipeline "https://www.youtube.com/@freecodecamp" --all --article-type learning

# 处理抖音单视频或博主主页合集
video2book pipeline "https://v.douyin.com/xxxx/" --article-type learning
video2book pipeline "https://www.douyin.com/user/MS4wLjAB..." --all --article-type learning
```

## 场景二：处理指定分集或区间

```bash
# 处理第 1 讲
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --page 1 --article-type learning

# 处理第 2 讲至第 5 讲
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --range 2-5 --article-type learning
```

## 场景三：生成模块合辑教材

在阶段一模块长文全部落盘后，整编生成模块教材（按块序整编成册，册=书、章=块）：

```bash
video2book cluster-articles "https://www.bilibili.com/video/BV14VqVBrEhc"
```

模块教材默认复用已有 `textbooks/`；需要按最新章节重编时加 `--force`：

```bash
video2book cluster-articles "https://www.bilibili.com/video/BV14VqVBrEhc" --force
```

## 场景四：生成思维导图复习笔记

```bash
# 复习笔记只有一种风格，无需 --style
# 归并（块 ➔ 成篇笔记）由 Agent 产出；缺归并不会卡住，命令始终正常退出
video2book cluster-notes "https://www.bilibili.com/video/BV14VqVBrEhc"

# --force：强制重导全部笔记任务书（已产出笔记成品的篇默认自动复用）
video2book cluster-notes "https://www.bilibili.com/video/BV14VqVBrEhc" --force
```

## 场景五：查看与监控任务队列状态

```bash
# 查看当前任务工作区的完成进度与阶段判定（含块级转录进度）
python scripts/queue_tracker.py

# 转录侧取载荷：待转录的块（块音频 / 块内时间表 / 逐字稿目标路径）
python scripts/queue_tracker.py --next-transcribe 2 --json

# 写作侧取载荷：只返回「块逐字稿已就绪且模块长文缺失」的块（转录与写作交错推进时用这个）
python scripts/queue_tracker.py --next-module 5 --json --log-dispatch

# 单行状态（含 STAGE1_DONE 与块级转录进度）
python scripts/queue_tracker.py --summary

# 多课程并存时指定工作区（否则取最近活动的那个）
python scripts/queue_tracker.py --pattern "微机原理" --next-module 5
```

## 场景六：交付前质检与收尾

```bash
# 长文依据级校验（模块长文是否真的基于本块逐字稿；默认提示级，--strict 才纳入门禁）
python scripts/article_grounding_check.py --strict

# 笔记成色体检（致命项：套话填充 / 空壳标题 / 分集平铺标题 / 行内残缺引用 / 分集口吻；
#              提示项：断句 / 结构缺件——加 --require-structure 才纳入门禁）
python scripts/note_quality_check.py --strict

# 渲染合规体检（致命项：GitHub 告警块 / 围栏外裸字符画 / 围栏配对；
#              提示项：围栏语言标识——加 --require-lang 才纳入门禁）
python scripts/render_compat_check.py --strict

# 任务书回收：成品产出后才回收，每类保留 1 份范本（先 --dry-run 预演）
python src/cli.py cleanup --dry-run
python src/cli.py cleanup

# 账本对账：以磁盘产物为唯一真相回填 manifest.json
python src/cli.py sync
```

> **任务书是临时派发物**：`*_TASK.md` 在成品产出后由 `cleanup`（或 pipeline 收尾）自动回收，
> 每个类别保留编号最小的 1 份作为提示词范本；`note_plan_TASK.md` 属课程级规划任务书，永不回收。

---

## 子命令全表

所有任务必须通过以下标准入口调用（功能一致，三选一均可）：

- 仓库推荐：`python src/cli.py <子命令>`
- 免安装脚本：`python scripts/run.py <子命令>`（可任意工作目录调用）
- 系统命令：`video2book <子命令>`（`pip install -e .` 后可用）

| 子命令 | 用途 |
| :--- | :--- |
| `parse` | 解析视频拓扑并列分集（B 站 / YouTube / 抖音 / 本地目录） |
| `audio` | 下载或抽取音频流 |
| `pipeline` | 阶段一主入口：取音频 → 装箱成块 → 导出转录与长文任务书 |
| `merge-audio` | 单独重跑装箱合并（幂等，可改块标题） |
| `split-transcript` | 可选：块逐字稿切回分集逐字稿（按集查阅） |
| `cluster-articles` | 按块序把模块长文整编成册（册=书、章=块） |
| `cluster-notes` | 块 → 笔记归并，导出笔记任务书 |
| `dedup` | 音频指纹去重，复用相同分集的语料与长文（0 Token） |
| `cleanup` | 回收已完成的任务书，每类保留编号最小的 1 份范本 |
| `sync` | 以磁盘产物为准回填 `manifest.json` |
| `info` | 环境与工具链就绪状态（含凭证来源与上次 412/熔断记录） |
| `login` | 持久化 B 站 `SESSDATA` / 抖音 Cookie |
| `logout` | 清除已保存的凭证 |

## 完整参数表

| 入口 | 参数 | 用途 |
| :--- | :--- | :--- |
| `parse` | `--limit N` / `--json` | 列表最多显示 N 条（默认 10）/ 输出 JSON |
| `audio` | `--page N` `--all` `--range X-Y` `--quality low\|medium\|high` `--url-only` `--output DIR` `--json` `--force` | 单集或批量取音频；`--url-only` 只打印直链不下载；`--output` 覆盖音频目录 |
| `pipeline` | `--all` `--range X-Y` `--page N` `--quality <档>` `--prefetch-workers N` `--skip-failed` `--block-minutes N` `--force` `--article-type <风格>` `--task NAME` `--base-dir DIR` | 阶段一主入口；`--skip-failed` 把音频失败集记入跳过名单继续跑；`--block-minutes` 是**块时长目标**（默认取 `BVB_AUDIO_BLOCK_MINUTES`，再默认 50，落进 40–60 带；硬上限看 `BVB_AUDIO_ONESHOT_LIMIT_MINUTES`）；`--force` 重派已完成块 |
| `merge-audio` | `<工作区目录>` `--block-minutes N` `--force` | 单独重跑音频装箱合并并重出块级转录任务书（幂等；改完 `block_titles.json` 后重跑即按新标题改名；`--force` 忽略指纹重建块） |
| `split-transcript` | `<工作区目录>` `--block N` | **可选动作**：把带时间戳的旧逐字稿切回分集（幂等；纯文本稿自动标记 unsplit 并保留块级稿，不影响写作） |
| `cluster-notes` | `--force` `--block-id N` `--start-block N` `--end-block N` | 块 → 笔记归并派发；后三个按**笔记序号**只处理指定区间（参数名是历史遗留）；`--force` 强制重导笔记任务书 |
| `cluster-articles` | `--force` | 默认复用已有教材，`--force` 按最新章节重编 |
| `dedup` | `--dry-run` | 只报告重复分集，不复制语料与长文 |
| `cleanup` | `--keep N`（默认 1） `--dry-run` `--task 关键字` `--all` | 每类保留 N 份任务书范本；`--all` 为兼容保留（不加即全量） |
| `sync` | `--dry-run` `--task 关键字` `--all` | 按磁盘对账回填 manifest |
| `note_quality_check.py` | `--strict` `--require-structure` `--max-truncated N`（默认 4） `--dir` `--task` `--base-dir` `--json` | 结构缺件默认只提示，`--require-structure` 才纳入门禁 |
| `render_compat_check.py` | `--strict` `--require-lang` `--dir` `--task` `--base-dir` `--json` | 围栏语言标识默认只提示，`--require-lang` 才纳入门禁 |
| `queue_tracker.py` | `--next-transcribe N` `--next-module N` `--next-note N` `--summary` `--json` `--dir PATH` `--pattern 关键字` `--base-dir DIR` `--log-dispatch` | 派发前取载荷：`--next-transcribe N`（转录侧：待转录的块）、`--next-module N`（写作侧：待写模块长文）、`--next-note N`（笔记侧：待写复习笔记），均自带预制 `dispatch_prompt`；`--summary` 额外给出 `BLOCKS/BLOCKS_TRANSCRIBED/TRANSCRIPT_READY`（就绪口径是块）；多课程并存时必须用 `--dir`/`--pattern`；`--log-dispatch` 追加派发台账（默认关闭） |
| `article_grounding_check.py` | `--strict` `--min-freq N`（默认 2） `--min-coverage F`（默认 0.5） `--dir` `--task` `--base-dir` `--json` | 依据级校验（**一块一验**）：块级逐字稿的技术实体在模块长文里的覆盖率，低于下限报警（默认提示级，`--strict` 才纳入门禁）；无逐字稿的块不参与判定 |
| `cleanup_tasks.py` | `--keep N` `--dry-run` `--task` `--json` `--strict` | `cleanup` 的独立脚本入口（功能一致） |

## 其余脚本入口

| 脚本 | 用途 | 常用参数 |
| :--- | :--- | :--- |
| `python scripts/strip_heading_numbers.py` | 存量产物的标题手写序号就地剥除（幂等） | `--dry-run` |
| `python scripts/selfcheck.py` | 仓库唯一门禁自检（技能自包含 + 多宿主声明 + 三域分离） | — |
| `python scripts/run.py <子命令>` | 免安装 CLI 入口，等价于 `python src/cli.py <子命令>` | 透传子命令 |

## 退出码

- `0` — 正常结束
- `1` — 通用错误 / 目标工作区缺失或参数非法
- `2` — 阶段一准备错误（音频下载未 100% 就绪或解析异常）
- `3` — 块级转录装箱/切分异常，或任务书导出失败
- `4` — 未确认长文提示词风格，即 `--article-type` 缺失或取值非法

---

## 环境与凭证

安装前置、平台对照与缺失处理见 [`install.md`](install.md)；
B 站 `SESSDATA` 凭证的两种配置方式与安全须知见 `SKILL.md` §2；
宿主工具名差异见 [`host-tools/`](host-tools/)。
