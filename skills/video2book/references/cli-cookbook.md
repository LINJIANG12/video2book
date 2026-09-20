# CLI 场景手册

本手册收录按任务目标划分的完整命令清单。`SKILL.md` 只给出主路径与入口导航，其余场景在此展开。

## 运行约定

- **工作目录 = 技能目录**（`SKILL.md` 所在目录，即 `skills/video2book/`）。下文所有 `python src/cli.py …` /
  `python scripts/…` 均以此为当前目录；
- 若已执行 `pip install -e .`，可直接使用 `video2book` 命令，等价于 `python src/cli.py`；
- 产物一律落在**产物根**，与代码目录分离；下文示例中的 `output/<task>/…` 均**相对产物根**；
  **产物根默认是「你跑命令时的工作目录」下的 `output/`**（在容器内工作时为 `<容器根>/output`）；
- 想固定位置：`--base-dir <路径>`，或环境变量 `BVB_OUTPUT_DIR`（产物根）/ `BVB_HOME`（容器根）。

---

## 场景一：处理整门课程流水线

```bash
# 处理整门 B 站网课合集（--article-type 必填，不传即退出码 4）
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

> `pipeline` 是**阶段一唯一入口**：收音频 → 装箱成块 → 自动去重 → 导出转录/长文任务书 → 自动回收任务书 + 对账。

## 场景二：处理指定分集或区间

```bash
# 处理第 1 讲
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --page 1 --article-type learning

# 处理第 2 讲至第 5 讲
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --range 2-5 --article-type learning
```

## 场景三：只解析拓扑 / 只收音频（轻量入口）

```bash
# 只解析拓扑并列出将处理的分集，不下载音频、不写任务书（原 `parse` 子命令）
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --all --dry-run

# 只收齐音频并装箱、导出块级转录任务书后返回（原 `audio` 子命令）
video2book pipeline "https://www.bilibili.com/video/BV14VqVBrEhc" --all --audio-only
```

> 去重由 `pipeline` 在音频收齐后**自动执行**（相同分集按 SHA-256 指纹复用既有语料与长文，0 Token），
> 不再有独立的 `dedup` 命令。

## 场景四：重新装箱 / 改动块标题

```bash
# 单独重跑装箱合并并重出块级转录任务书（幂等）
video2book merge-audio "<产物根>/<课程工作区>"

# 忽略指纹强制重建块
video2book merge-audio "<产物根>/<课程工作区>" --force

# 改块标题：编辑 <工作区>/audio/_blocks/block_titles.json 后重跑，即按新标题改名
video2book merge-audio "<产物根>/<课程工作区>"
```

## 场景五：生成思维导图复习笔记 + 模块合辑教材

```bash
# 笔记归并（块 → 成篇笔记）：导出 note_plan_TASK.md，Agent 写 note_plan.json 后重跑即派发
# 缺归并不会卡住，命令始终正常退出；收尾自动 cleanup + sync
video2book cluster-notes "https://www.bilibili.com/video/BV14VqVBrEhc"
video2book cluster-notes "https://www.bilibili.com/video/BV14VqVBrEhc" --force   # 强制重导笔记任务书

# 教材整编（按块序整编成册，册=书、章=块）；默认复用已有教材，按最新章节重编加 --force
video2book cluster-articles "https://www.bilibili.com/video/BV14VqVBrEhc"
video2book cluster-articles "https://www.bilibili.com/video/BV14VqVBrEhc" --force
```

## 场景六：质量门禁（阶段一放行 / 交付前体检 / 存量标题去号）

```bash
# 阶段一放行门禁：模块长文是否真的基于本块逐字稿（默认提示级，--strict 才纳入门禁）
python src/cli.py check --stage1 --strict
python src/cli.py check --stage1 --strict --min-coverage 0.6      # 调整覆盖率下限

# 交付前体检（默认）：笔记成色 + 渲染合规
#   笔记致命项：套话填充 / 空壳标题 / 分集平铺标题 / 行内残缺引用 / 分集口吻
#   渲染致命项：GitHub 告警块 / 围栏外裸字符画 / 围栏配对
python src/cli.py check --deliver --strict
python src/cli.py check --deliver --strict --require-structure    # 结构缺件纳入门禁
python src/cli.py check --deliver --strict --require-lang         # 围栏缺语言标识纳入门禁
python src/cli.py check --deliver --strict --require-no-numbering # 标题手写序号纳入门禁

# 存量产物标题手写序号就地清理（幂等；先 --dry-run 预演）
python src/cli.py check --fix-numbering --dry-run
python src/cli.py check --fix-numbering

# 多工作区并存时用 --task / --dir / --base-dir 定位
python src/cli.py check --deliver --strict --task 微机原理
python src/cli.py check --stage1 --dir "<工作区绝对路径>"
```

## 场景七：查看与监控任务队列状态

```bash
# 查看当前任务工作区的完成进度与阶段判定（含块级转录进度）
python scripts/queue_tracker.py

# 转录侧取载荷：待转录的块（块音频 / 块内时间表 / 逐字稿目标路径）
python scripts/queue_tracker.py --next-transcribe 2 --json

# 写作侧取载荷：只返回「块逐字稿已就绪且模块长文缺失」的块
python scripts/queue_tracker.py --next-module 5 --json --log-dispatch

# 笔记侧取载荷
python scripts/queue_tracker.py --next-note 5 --json

# 单行状态（含 STAGE1_DONE 与块级转录进度）
python scripts/queue_tracker.py --summary

# 多课程并存时指定工作区（否则取最近活动的那个）
python scripts/queue_tracker.py --pattern "微机原理" --next-module 5
```

## 场景八：收尾（一般无需手动执行）

```bash
# 以下两条已由 pipeline 与 cluster-* 收尾自动执行；仅在需要单独复算时手动跑
python src/cli.py cleanup --dry-run    # 任务书回收预演（成品产出后才回收，每类保留 1 份范本）
python src/cli.py cleanup              # 真正回收
python src/cli.py sync                 # 以磁盘产物为唯一真相回填 manifest.json
```

> **任务书是临时派发物**：`*_TASK.md` 在成品产出后由 `cleanup` 自动回收，每个类别保留编号最小的 1 份作为提示词范本；
> `note_plan_TASK.md` 属课程级规划任务书，永不回收。

---

## 子命令全表

所有任务必须通过以下标准入口调用（功能一致，三选一均可）：

- 仓库推荐：`python src/cli.py <子命令>`
- 免安装脚本：`python scripts/run.py <子命令>`（可任意工作目录调用）
- 系统命令：`video2book <子命令>`（`pip install -e .` 后可用）

| 子命令 | 用途 |
| :--- | :--- |
| `pipeline` | 阶段一唯一入口：`--dry-run` 只解析 / `--audio-only` 只取音装箱 / 默认跑完整链路并自动收尾 |
| `merge-audio` | 单独重跑装箱合并（幂等，可改块标题） |
| `cluster-notes` | 块 → 笔记归并，导出笔记任务书（收尾自动 cleanup + sync） |
| `cluster-articles` | 按块序把模块长文整编成册（册=书、章=块；收尾自动 cleanup + sync） |
| `check` | 质量门禁：`--stage1` 依据级校验 / `--deliver` 交付前体检 / `--fix-numbering` 存量标题去号 |
| `cleanup` | 回收已完成的任务书，每类保留编号最小的 1 份范本 |
| `sync` | 以磁盘产物为准回填 `manifest.json` |
| `info` | 环境与工具链就绪状态（含凭证来源与上次 412/熔断记录） |
| `login` | 持久化 B 站 `SESSDATA` / 抖音 Cookie |
| `logout` | 清除已保存的凭证 |

## 完整参数表

| 入口 | 参数 | 用途 |
| :--- | :--- | :--- |
| `pipeline` | `--all` `--range X-Y` `--page N` `--quality <档>` `--prefetch-workers N` `--skip-failed` `--block-minutes N` `--force` `--article-type <风格>` `--dry-run` `--audio-only` `--task NAME` `--base-dir DIR` | 阶段一主入口；`--skip-failed` 把音频失败集记入跳过名单继续跑；`--block-minutes` 是**块时长目标**（默认取 `BVB_AUDIO_BLOCK_MINUTES`，再默认 50，落进 40–60 带；硬上限看 `BVB_AUDIO_ONESHOT_LIMIT_MINUTES`）；`--force` 重派已完成块 |
| `merge-audio` | `<工作区目录>` `--block-minutes N` `--force` | 单独重跑音频装箱合并并重出块级转录任务书（幂等；改完 `block_titles.json` 后重跑即按新标题改名） |
| `cluster-notes` | `--force` `--block-id N` `--start-block N` `--end-block N` | 块 → 笔记归并派发；后三个按**笔记序号**只处理指定区间（参数名是历史遗留）；`--force` 强制重导笔记任务书 |
| `cluster-articles` | `--force` | 默认复用已有教材，`--force` 按最新章节重编 |
| `check` | `--stage1` `--deliver` `--fix-numbering` `--strict` `--dir` `--task` `--base-dir` `--json` `--min-freq N`(2) `--min-coverage F`(0.5) `--max-truncated N`(4) `--require-structure` `--require-lang` `--require-no-numbering` `--only {textbooks,articles,both}` `--dry-run` `--max-samples N`(5) `--hash-nonheading` | `--stage1` 依据级校验（块级逐字稿技术实体在模块长文里的覆盖率）；`--deliver`（默认）笔记成色 + 渲染合规；`--fix-numbering` 存量标题去号；默认提示级，`--strict` 才纳入门禁 |
| `cleanup` | `--keep N`（默认 1） `--dry-run` `--task 关键字` `--all` | 每类保留 N 份任务书范本；`--all` 为兼容保留（不加即全量） |
| `sync` | `--dry-run` `--task 关键字` `--all` | 按磁盘对账回填 manifest |
| `queue_tracker.py` | `--next-transcribe N` `--next-module N` `--next-note N` `--summary` `--json` `--dir PATH` `--pattern/--task 关键字` `--base-dir DIR` `--log-dispatch` | 派发前取载荷：三种载荷均自带预制 `dispatch_prompt`；`--summary` 额外给出 `BLOCKS/BLOCKS_TRANSCRIBED/TRANSCRIPT_READY`（就绪口径是块）；`--log-dispatch` 追加派发台账（默认关闭） |

## 其余脚本入口

| 脚本 | 用途 | 常用参数 |
| :--- | :--- | :--- |
| `python scripts/queue_tracker.py` | 阶段门禁与派发载荷（派发中枢） | 见上表 |
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
运行依赖、两条听音通道与凭证获取见 [`runtime.md`](runtime.md)；
宿主工具名差异见 [`host-tools/`](host-tools/)。
