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

## 环境与凭证

安装前置、平台对照与缺失处理见 [`install.md`](install.md)；
B 站 `SESSDATA` 凭证的两种配置方式与安全须知见 `SKILL.md` §2；
宿主工具名差异见 [`host-tools/`](host-tools/)。
