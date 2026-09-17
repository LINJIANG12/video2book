---
name: video2book
description: 把 B 站、YouTube、抖音长视频/系列网课或本地音视频重构为精读教材长文、模块合辑全书与思维导图复习笔记的完整技能，自带多平台统一媒体内核、音频提取与块级转录（把连续几集拼成块、按块一次转录后按时间表切回分集逐字稿）、两趟语义聚合、双通道听音（回退链路）与多阶段门禁工具链。当用户提出「把网课/视频做成教材」「整理成复习笔记或思维导图」「这门课帮我精读一遍」「B 站/油管/抖音这个合集重构成文档」，或给出本地课程目录要求系统化整理时，使用本技能。
license: MIT
metadata:
  author: LINJIANG12
  version: 2.6.0
  category: learning-and-education
  compatibility: Python 3.10+；系统 ffmpeg 在 PATH；宿主需具备 read_audio 或 read_media 听音通道之一。
---

# Video2Book: 视频网课重构教材与复习笔记 Skill

本 Skill 面向**任意支持 Agent Skills 规范的宿主**（Claude Code、Codex、OpenCode 等）与系统终端，用于将 B 站、YouTube、抖音长视频/系列网课或本地音视频转换为结构化技术教材、模块合辑全书与思维导图复习笔记。平台差异（安装位置、工具名）见 `references/install.md` 与 `references/host-tools/`。

---

## 1. 核心原则：黑盒编排与工具调用者公约 (Black-Box Operator Contract)

执行本 Skill 的 AI 模型必须时刻牢记自身定位为**高层流水线编排者（Orchestrator）与内容创作者**，严禁以“程序员重构源码”的心态处理任务：

> [!IMPORTANT]
> **【五条不可逾越的执行红线】**：
> 1. **黑盒调用原则（严禁窥探与私造脚本）**：
>    - 严禁阅读或修改底层实现源码（如 `src/` 内部代码）来寻找“捷径”；
>    - **严禁编写任何 `gen_*.py` 等离线批量造文脚本**来伪造、填充产物。所有任务必须通过官方 CLI 命令与原生多模态工具链推进；
> 2. **逐字稿事实保真原则（Strict Transcript Grounding）**：
>    - 所有 `articles/PXX_*.md` 的撰写，必须建立在**本集逐字稿**的基础之上。默认链路是**块级转录**：
>      专职转录角色按**块**取音（一块覆盖连续的几集，块的装箱与拼接见 §4.2），产出块级逐字稿后
>      按块内时间表切成 `subtitles/PXX_<标题>_逐字稿.md`；**写作角色读逐字稿写长文，不再听音频**；
>    - 已有 `subtitles/PXX_*_clean.txt`（人工清洗稿或旧链路的逐集文本）的集**直接复用**那份语料，
>      不为了统一格式把已经存在的逐字稿再转录一遍（那是纯烧钱）；
>    - 回退链路（`pipeline --no-merge`）仍走「逐集取音」，那时按宿主能力二选一（先看自己的工具列表里有哪个，不要猜）：
>      - **有原生音频模态**（工具列表里有 `read_audio`）→ 调用 `read_audio(output_mode="file")` 提取切片，再用**宿主自己的文件查看能力**（能直接感知音频内容的那件工具，各平台工具名见 `references/host-tools/`）真正聆听；
>      - **没有原生音频模态**（只有 `read_media`）→ 调用 `read_media` 由**外部模型代读**，取回逐字稿/讲解文本作为事实依据；
>      - 两条通道的选择规则与分页契约见 §4.2；**无论走哪条，都必须拿到本集真实讲解内容**，不得跳过取语料这一步直接编造；
>    - 正文必须包含讲师亲口讲述的真实案例、例题或板书比喻（Grounding Evidence），严禁脱离逐字稿凭空脑补；
>    - **这一条从「纪律」升级为可校验项了**：`scripts/article_grounding_check.py` 用逐字稿的技术实体
>      （英文标识符与多位数字）在长文里的覆盖率报警，低于下限即指出「这篇没怎么用语料」（见 §4.5）；
> 3. **拒绝脱缰黑话（Context Preservation）**：
>    - 高校经典基础课（如数据结构、操作系统、数据库）严禁脱离课程实际，生搬硬套互联网大厂“微服务”、“分布式架构师”、“NVMe SSD”等浮夸黑话。
> 4. **提示词风格红线（Prompt-Style Gate）**：
>    - 当前提供两种长文风格：**`learning`「学习」（推荐，当前版）** 与 **`legacy`「旧版」（原稳定版，教材腔、随堂自测）**；两者的具体差别见 § 4.0；
>    - **风格由用户确认**：`pipeline` / `transcribe` 都必须带 `--article-type`；未指定时工具层打印风格菜单并在交互终端请用户当场选择，
>      仍然确认不了就**以退出码 4 终止任务**（工具层不猜、不兜底）；
>    - 另有咨询答疑 / 访谈对谈 / 测评体验 / 直播闲聊四种形态只登记、未提供提示词：命中即终止；
>      **严禁换个名字硬套、严禁手工套用别的提示词继续写**。
> 5. **阶段一派发纪律（执行者约束，2026-09 起；2026-09 块级转录版）**：
>    - **课程总时长 ≤ 60 分钟** → 主 Agent 可串行亲做（取语料 + 写作都在主上下文里完成）；
>    - **总时长 > 60 分钟** → **必须派发**。默认链路按**两类角色**分工，主 Agent 只做调度：
>      - **转录角色（专职，建议 2 个）**：只做音频转录这一件事，各自连续消费自己那批块
>        （取载荷：`queue_tracker.py --next-transcribe N`），**不回传正文**，只回报一行；
>      - **写作角色（持续派发）**：按块领任务、读块内各集逐字稿写长文
>        （取载荷：`queue_tracker.py --next-article N`，只返回逐字稿已就绪且长文缺失的集）；
>      - 两类角色**交错推进**：先推一波转录，逐字稿一就绪就派写作，不要等全部转录结束才开工；
>    - 回退链路（`--no-merge`）下的写作粒度：**一集一子智能体**；当「集数 ≥ 15 且单集预算 ≤ 40k token」时，
>      按 `suggest_batch` 建议改为 **3~5 集打包给一个子智能体**（省派发协调开销，代价是返修粒度变粗）；
>    - **窗口兜底（仅回退链路的通道 A 适用）**：实算音频 token（时长 × 系数）超过窗口 60% 时，即使不足 60 分钟也必须派发。
>      音频 token 系数与窗口**随宿主而异**，可用 `BVB_AUDIO_TOKENS_PER_SEC`（默认 `32`，Gemini 原生音频口径；
>      OpenAI input_audio 约 `100`）与 `BVB_CONTEXT_WINDOW_TOKENS`（默认 `1000000`）覆盖；工具会打印实算值；
>      走**通道 B**（外部模型代读，见 § 4.2）时音频根本不进宿主上下文，这条预算口径不适用，以外部模型的额度为准；
>      **块级转录链路的写作侧不吃音频**（只吃逐字稿文本），所以这条预算只在转录侧成立；
>    - **主 Agent 不得代听代写**（除上两条兜底），只负责取载荷、派发、跑门禁；
>    - 宿主不具备子智能体能力时，显式声明「单集串行模式」，每 5~8 集换新会话，**不得**因此跳过红线 2（逐字稿保真）；
>    - **门禁放行**：全部长文完成后先用脚本验收（`queue_tracker.py --summary` 看 `STAGE1_DONE=1`，
>      再跑 `article_grounding_check.py` / `note_quality_check.py` / `render_compat_check.py`），
>      **合格才允许派发后续模块任务**；脚本判不了的内容质量仍靠抽样复核兜底；
>    - **纪律与门禁的边界**：执行者身份、谁写的，工具层**无法校验**（见 § 4.5）；
>      但「长文是否基于本集逐字稿」已从纪律升级为可校验（`article_grounding_check.py`）。

---

## 2. 凭证配置：登录凭证（B 站 SESSDATA / 抖音 Cookie）

两类平台各有自己的登录凭证，**都通过同一个 `login` 命令持久化**。本地音视频、以及与你所用平台无关的任务自动跳过相应项。

### 2.1 B 站任务 SESSDATA（建议提供）

当处理 B 站（bilibili.com 或 BV 号）合集任务时，建议提供 `SESSDATA` 登录凭证，以保障高并发抓取稳定性并避免触发 412 频控限制。

**获取方式**
1. 浏览器访问 bilibili.com 登录；
2. 按 `F12` 打开开发者工具 -> Application（应用） -> Cookies -> `https://www.bilibili.com`；
3. 复制 `SESSDATA` 对应的值。

### 2.2 抖音任务 Cookie（**必须优先向用户索取**）

> [!IMPORTANT]
> **处理抖音目标（`douyin.com` / `v.douyin.com` 短链）时，Agent 必须先向用户索取 Cookie，再开始抓取。**
> 这不是"可选优化"——不提供会让抓取结果**静默残缺**。

**为什么必须先问**：抖音对**匿名**访问施加作品列表硬窗口。实测某摄影博主主页显示 **216** 条作品，
匿名抓取只放行 **21** 条，且翻页在第二页直接返回空列表；合集接口返回 **403**；
主页 HTML 是纯 JS 壳，没有任何内联数据。**免 cookie 无任何可行绕行方案**。
由于产物是按"实际取到的分集"生成的，缺失的部分不会出现在教材／笔记里——
使用者若不知情，会误以为已经抓全。

**索取话术（照此向用户说明，不要省略风险）**

1. **先要凭证**，并附上获取指引：
   > 抖音需要登录态 Cookie 才能取到全部作品。匿名访问只放行约 20 条
   > （实测某博主 216 条只取到 21 条），且合集接口会返回 403，没有免 cookie 的办法绕过。
   > 获取方式：浏览器登录 douyin.com → 按 `F12` → Application（应用）→ Cookies →
   > `https://www.douyin.com` → 复制**整串** Cookie 值。
   > 提供后我可以持久化保存（`login --douyin-cookie "<串>"`），后续任务不必重复提供。
2. **用户拒绝或暂时不便提供** → **明确说明风险，但可以继续执行**：
   > 明白。那我按匿名方式继续——请注意这次只可能抓到约 20 条作品，
   > 其余分集不会出现在教材和笔记里。若之后想补全，重新提供 Cookie 再跑一次即可
   > （已抓到的音频与长文会被复用，不会重复下载）。
   随后照常推进任务，**不得**因为缺凭证而卡住或终止。

**使用方式（二选一）**
1. **一次性**：命令追加 `--douyin-cookie "<Cookie 串>"`，仅作用于本次执行；
2. **持久化（推荐）**：执行一次 `python src/cli.py login --douyin-cookie "<Cookie 串>"`，之后所有命令自动使用。

凭证优先级（抖音）：`--douyin-cookie` > 本地存档 / 环境变量 `$DYAUDIO_COOKIE` > 配置文件的 `cookie` 字段。

### 2.3 通用命令

```bash
python src/cli.py login --sessdata "<SESSDATA>"            # 保存 B 站凭证
python src/cli.py login --douyin-cookie "<Cookie 串>"       # 保存抖音凭证
python src/cli.py info                                     # 查看各凭证来源与脱敏指纹
python src/cli.py logout                                   # 撤销保存（两类一起清除）
```

> [!WARNING]
> **凭证安全须知**：持久化的凭证以明文存于产物根下的 `.sessdata.json` / `.douyin_cookie.json`，
> 该路径已被 `.gitignore` 排除（另有显式规则兜底），不会进入版本库；命令行参数的优先级始终高于本地存档。
> 这些凭证等同你的平台登录态，请勿复制、上传或分享；若怀疑泄露，请到对应平台退出登录使其失效，
> 并运行 `logout` 清除本地存档。

---

## 3. 标准作业流程 (Standard Operating Procedures - SOP)

> [!IMPORTANT]
> **运行前置（技能目录与工作目录契约）**：本仓库是**插件/分发单元**；技能本体与它依赖的工具链**自包含**在同一个目录里，**安装这一个目录即可**。**容器根是可选的**，两种用法都支持：
>
> ```text
> ① 容器布局（存在 $BVB_HOME 或祖先目录里的 .bvb-home 标记时沿用）
> <容器根>/
> ├── skill/skills/video2book/   ← 本技能：SKILL.md + references/ + src/ + scripts/（安装单元）
> ├── omni-media/                ← 可选：两个音视频 MCP 服务的仓库（mcp/ 原生听音、mcp-ext/ 外部模型代读）
> └── output/                    ← 产物根：各课程工作区 + .sessdata.json / .wbi_keys.json / .cli_status.json
>
> ② 默认（没有任何容器标记时）
> <你的工作目录>/output/          ← 产物落在这里，不需要配置任何东西
> ```
>
> - **产物根默认取「你的工作目录」下的 `output/`**；只有当容器标记存在**且你就在该容器内工作**时才改用 `<容器根>/output`——标记是从技能所在位置向上找的，所以把技能软链进平台技能目录后，从别的项目调用仍按你的工作目录解析。要钉死位置就用 `--base-dir <路径>` 或 `BVB_OUTPUT_DIR`。
> - **所以请在「你要放产物的那个工作目录」下执行命令**：用绝对路径调用 CLI（`python "<技能目录>/src/cli.py" …`）即可；相对形式 `python src/cli.py` 要求 cwd 是技能目录，那样产物会跟着落到技能目录下——除非显式传 `--base-dir`。
> - **听音通道不需要配置**：`read_audio` / `read_media` 由宿主的 MCP 提供，**一律由 Agent 在用时按自己的工具列表判定**（见 §4.2）；配套 MCP 仓库装在哪只影响 `info` 里的位置提示，不影响通道是否可用。
> - **安装/挂载时把整个技能目录一起带走**（`SKILL.md` 与 `src/`、`scripts/` 同在），不要只复制 `SKILL.md`；各平台装到哪、怎么装，见 `references/install.md`。

整个重构流水线分为一个准备阶段与两个核心阶段，Agent 只需按顺序执行指定命令与工具：

```text
[输入 URL 或本地课程目录]
          │
          ▼
【第 -1 步：平台凭证检查（抖音目标不可跳过）】
  目标是抖音（douyin.com / v.douyin.com 短链）？
  ├── 是 ➔ **先向用户索取 Cookie**（附获取指引，话术见 §2.2）
  │        ├── 用户提供 ➔ login --douyin-cookie "<串>" 持久化后继续
  │        └── 用户拒绝/不便 ➔ **明确说明风险**（只能抓到约 20 条，其余分集不会进产物），
  │                          但**照常继续执行**——不得因缺凭证而卡住或终止
  └── 否（B 站 / YouTube / 本地）➔ 跳过（B 站建议提供 SESSDATA，见 §2.1）
          │
          ▼
【第 0 步：确认长文提示词风格（工作流启动前，不可跳过）】
  从风格菜单中选一种 ➔ 显式传 --article-type <风格键>（或由工具在交互终端当场询问）
  ├── learning「学习」（推荐，当前版）／ legacy「旧版」（原稳定版）➔ 继续
  └── 未指定且确认不了 / 拼写错误 / 命中未提供提示词的形态
        ➔ 打印风格菜单 + exit 4 终止任务（不猜、不降级、不硬套）
          │
          ▼
【准备阶段：结构解析 · 音频收齐 · 装箱成块】
  python src/cli.py pipeline "<链接或路径>" [--all | --range X-Y] --article-type learning [--sessdata "..."]
  ├── 解析分 P 结构 (parts.json)
  ├── 流式下载 16kHz 单声道音频至 audio/
  ├── 装箱：按**集边界**把连续几集拼成块 → audio/_blocks/BLK01_P08-P12.m4a + blocks.json
  │      （块时长目标可配：--block-minutes N 或 BVB_AUDIO_BLOCK_MINUTES，默认 60 分钟；
  │        单块硬上限 BVB_AUDIO_ONESHOT_LIMIT_MINUTES，默认 75 分钟——超过会被取音侧自动分卷）
  │      ※ 块只用于「按块转录、少调用几次取音接口」；**一集绝不劈进两块**；
  │        不需要装箱时加 --no-merge 回退「逐集取音」老链路
  ├── 导出块级转录任务书：subtitles/BLK01_P08-P12_转录任务书.md（内含块内每集的起止时间表）
  └── 可选去重：python src/cli.py dedup "<链接或路径>"（pipeline 不会自动调用，需手动执行）
          │
          ▼
【阶段一 A：块级转录（由**专职转录角色**承担，建议 2 个角色并行消费块队列，只做这一件事）】
  转录角色取载荷：python scripts/queue_tracker.py --next-transcribe 2 --json
  ├── 1. 读块：subtitles/BLK01_P08-P12_转录任务书.md（块音频绝对路径 + 块内时间表）
  ├── 2. 转录：read_media(file_path=块音频, mode="transcribe", duration_minutes=块时长)
  │      并把任务书 2.1 节那段「行首 [HH:MM:SS] 时间戳」要求**原样**传进 instruction
  │      （缺了它，块级逐字稿无法机械切回分集）
  ├── 3. 落盘：完整转录正文写入 subtitles/BLK01_P08-P12_逐字稿.md
  ├── 4. 切分：python src/cli.py split-transcript "<工作区目录>" --block 1
  │      按块内时间表机械切成 subtitles/PXX_<标题>_逐字稿.md（不需要手工誊抄）
  │      ※ 报「边界未锚定 / 空集 / suspect」说明归属不可靠：按 2.1 节重读该块后重跑本命令
  └── 5. 回报一行：BLK01 | 逐字稿路径 | 字节数 | 切分结果（**不回传正文**）
          │
          ▼
【阶段一 B：单集教材长文直出（派发回路；逐字稿一就绪就派写作，不必等全部转录完）】
  主 Agent 取载荷：python scripts/queue_tracker.py --next-article 5 --log-dispatch --json
  （只返回「逐字稿已就绪且长文缺失」的集；载荷含 任务书路径 / 逐字稿路径 / 目标长文路径，禁止手抄路径）
  ├── 1. 派生：一个子智能体领一个块、依次写块内各集长文，并发 5~6
  ├── 2. 子智能体读语料：打开发给它的本集逐字稿（任务书第 1 节给了绝对路径）
  │      ※ 逐字稿未就绪时**立即停止并回报**，不得凭分集标题编造、也不得去别处找音频补听
  ├── 3. 子智能体撰写教材：依逐字稿实际讲解内容撰写深入技术长文 (载入所选风格提示词)
  ├── 4. 子智能体落盘：用宿主的文件写入能力写入 articles/PXX_*_精读文章.md（严格保留，严禁八股模板）
  ├── 5. 子智能体回报一行：P07 | 文件路径 | 字节数 | 执行者（**不回传正文**）
  └── 6. 主 Agent 验收门禁：python scripts/queue_tracker.py --summary 确认 STAGE1_DONE=1；
         再跑 python scripts/article_grounding_check.py --strict 核对长文确实基于逐字稿；
         两关都过才放行阶段二（脚本判不了的内容质量靠抽样复核兜底）
          │
          ▼
【阶段二：两趟语义聚合（模块 ➔ 笔记）＋ 教材整编，需 Agent + 子智能体往返】
  单集文章全部就绪后，通过官方 CLI 聚合（严禁手工写脚本拼接）：
  ├── ① 模块规划（第一趟）：python src/cli.py cluster-notes "<链接>"
  │      导出 topic_plan_TASK.md ➔ Agent 写入 topic_plan.json（消费方＝教材）
  ├── ② 笔记归并（第二趟）：重跑同一命令，导出 note_plan_TASK.md
  │      ➔ Agent 写入 note_plan.json：把模块归并成若干篇笔记（一篇可跨多个模块）
  ├── ③ 笔记派发：重跑后自动逐篇导出 notes/笔记XX_*_TASK.md
  │      ➔ 主 Agent 派子智能体（一篇笔记一个）逐篇读完该篇涵盖的 articles/ 后撰写笔记
  │      ※ 两趟缺规划都不停机：越界块裁掉、无人认领的集号补占位、缺规划用兜底粒度继续
  ├── 模块合辑教材：python src/cli.py cluster-articles "<链接或路径>"  --> 生成 textbooks/
  ├── 质检门禁（手动体检，不在流水线上拦人）：python scripts/note_quality_check.py --strict（笔记成色）
  │                                          python scripts/render_compat_check.py --strict（渲染合规）
  ├── 收尾：python src/cli.py cleanup（回收任务书，每类留 1 份范本）
  │        python src/cli.py sync（按磁盘对账回填 manifest）
  └── 交付纪律：articles/ 下单集教材长文必须 100% 完整保留，供逐讲查阅
```

---

## 4. 阶段一：单集教材长文直出执行规范

### 4.0 长文提示词风格（用户确认，工作流第一步）

当前提供两种长文写出风格，**由用户确认后使用**。跑 `pipeline` / `transcribe` 时显式传 `--article-type`；不传则打印菜单并在交互终端询问，确认不了即终止任务：

| 风格键 | 名称 | 状态 |
| :--- | :--- | :--- |
| `learning` | **学习** | **推荐（当前版）** |
| `legacy` | **旧版** | 已提供（逐字保留） |
| `consulting` / `interview` / `review` / `livestream` | 咨询答疑 / 访谈对谈 / 测评体验 / 直播闲聊 | 已登记，**提示词未提供**（命中即终止） |

未指定类型、拼写无法命中、或判为「提示词未提供」的类型时，命令会**打印类型菜单并以退出码 4 终止任务**，不会落盘任何任务书。此时既不要换个类型名重试，也不要手工套用别的提示词继续写。

命中的类型会写进任务书抬头（`> 长文风格：…`）并记入 `manifest.json` 的 `article_type` 字段，便于回查这一篇是按哪套提示词写的。

> **标题纪律（长文 / 教材 / 笔记统一）**：标题**一律不写序号**。阅读器（Typora）会自动给标题编号，
> 手写序号会与它叠成 `1.1.` / `1. 第 1 章：…` 这种双号——笔记与模块教材各踩过一次，所以
> 笔记提示词写明「标题里一律不要写序号」，长文提示词同理，教材整编则在降级前**幂等去号**
> （把继承自旧长文的 `## 2.1 …`、`## 第 N 章：…` 剥掉）。存量产物用
> `python scripts/strip_heading_numbers.py --dry-run` 先预演、再去掉 `--dry-run` 就地清理。

### 4.1 任务来源

阶段一由 `pipeline` 导出**两类任务书**，各管一段：

| 任务书 | 位置 | 交给谁 | 内含 |
| :--- | :--- | :--- | :--- |
| **块级转录任务书** | `subtitles/BLK01_P08-P12_转录任务书.md` | **专职转录角色** | 块音频绝对路径、块内每集起止时间表、逐字稿目标路径、时间戳要求、切分命令 |
| **单集长文任务书** | `articles/PXX_*_TASK.md` | 写作角色（子智能体） | 本集逐字稿路径（**唯一事实来源**）、目标长文路径、所选类型的文章撰写提示词 |

单集长文任务书**不再夹带音频切片清单**（那是回退链路 `--no-merge` 的形态）：逐字稿链路的
写作侧只吃文本，音频留在块里、由转录角色消费。任务书第 1 节会写明「本集逐字稿（唯一事实来源）」
与「所属块音频（备查，不必再听）」，并给出前置条件：**逐字稿缺失就立即停止并回报**，不得编造。

### 4.2 工具链调用链路

**默认链路是「块级转录 → 分集逐字稿 → 读逐字稿写长文」**，取音只发生在转录角色身上：

- 块由 `pipeline` 在收齐音频后自动装箱（按集边界拼连续几集，目标时长
  `--block-minutes` / `BVB_AUDIO_BLOCK_MINUTES`，默认 60 分钟；单块硬上限
  `BVB_AUDIO_ONESHOT_LIMIT_MINUTES`，默认 75 分钟——超过取音侧一次性就绪阈值会被自动分卷，
  调用次数反而回升）。清单落在 `audio/_blocks/blocks.json`，是切分逐字稿的**唯一事实源**；
- 转录角色按块调用 `read_media(mode="transcribe")`（**必须**把任务书 2.1 节的
  「行首 `[HH:MM:SS]` 时间戳」要求原样传进 `instruction`），把整块正文写入
  `subtitles/BLK01_P08-P12_逐字稿.md`；
- 切分由工具层做，不需要手工誊抄：`python src/cli.py split-transcript "<工作区>" --block 1`
  按块内时间表把块级稿切成 `subtitles/PXX_<标题>_逐字稿.md`。切分是**确定性**的：
  时间戳落在哪一集的时间区间里就归哪一集。异常会在**放行边界**被拦住：
  **边界未锚定**、切后出现空集，或某集内容占比达到时长占比的 1.5 倍以上时，整块标记
  `suspect`；已有分集文件保留供排障，但不会进入写作派发。**`unsplit`**（完全没有时间戳）
  同样不切分、保留块级稿。以上状态都要求按任务书 2.1 节重读该块后重跑切分命令；
- 已有 `subtitles/PXX_*_clean.txt`（人工清洗稿或旧链路逐集文本）的集**直接复用**，
  写作角色照它写长文，不重复转录；
- **改块时长要留意**：块编号与集号区间由「目标时长 + 集时长分布」决定，改一次
  `--block-minutes` 就可能把 15 块变成 12 块。重跑时会自动**作废与本次装箱不符的旧转录任务书**
  （否则它是一份可被派发的幽灵任务），块音频与块级逐字稿只报告不删——它们可能仍被
  已切出的分集逐字稿引用。

**回退链路（`pipeline --no-merge`）** 才使用下面的两条听音通道，按宿主的原生音频能力二选一。
判断依据是**你自己的工具列表**，不要猜：有 `read_audio` 就走原生，只有 `read_media` 就走外部模型代读。

| | 通道 A：宿主原生听音（`omni-media`） | 通道 B：外部模型代读（`omni-media-ext`） |
| :--- | :--- | :--- |
| 适用 | 宿主模型本身具备音频模态（Gemini / GPT-4o Audio / Codex 等） | 宿主只有文本能力 |
| 工具 | `read_audio` | `read_media` |
| 凭证 | 零凭证 | 需要 `config.json` 里的外部模型端点与 api_key |
| 成本 | 无额外调用费 | 按外部模型计费 |
| 产出 | 本地切片路径（还要宿主自己听） | 直接是逐字稿/总结文本 |

**通道 A（有 `read_audio` 时优先）**：

1. **取切片**：对任务书清单中的切片调用 MCP 工具 `omni-media:read_audio`：
   ```json
   {
     "file_path": "<task_dir>/audio/P01_xxx.m4a",
     "output_mode": "file"
   }
   ```
   任务书里的切片本就是按 **60 分钟预算**切好的（每片 ≤ 60 分钟，`omni-media` 对 ≤ 75 分钟文件一次性整片就绪），
   因此**不要传 `duration_minutes`**，一次听完整片即可；只有返回文本里 `OMNI_STATUS` 显示 `is_finished=false`（超长媒体自动分卷）时，
   才用返回的 `start_time` / `duration_minutes` 续读下一卷；
2. **多模态感知**：用**宿主自己的文件查看能力**（能直接感知音频内容的那件工具；各平台工具名见 `references/host-tools/`）打开切片绝对路径，直接聆听讲师原声、例题推导与板书讲解；超长音频按返回的续读参数逐片听完；

**通道 B（只有 `read_media` 时）**：

1. **代读**：对任务书清单中的切片调用 MCP 工具 `omni-media-ext:read_media`：
   ```json
   {
     "file_path": "<task_dir>/audio/P01_xxx.m4a",
     "mode": "transcribe"
   }
   ```
   返回的是**文本**，不是音频：`transcribe` 给逐字稿，`summarize` 给教材级总结，
   `qa` 给带时间范围佐证的问答。外部模型端点由 `omni-media/mcp-ext/config.json` 决定，可用 `endpoint` 参数按名切换。
   **切片沿用任务书切好的粒度**，与通道 A 同一套规则：不要自己另填一个切片长度（填小了会把一集拆成十几次调用，
   填大了会被载荷预算收窄）；只有返回文本里 `OMNI_STATUS` 显示 `is_finished=false` 时，
   才按其中的 `next_start_time` / `next_duration_minutes` 续读下一片；
2. **续读同构**：返回文本首行的 `OMNI_STATUS` 注释与通道 A **同名同义**
   （`contract_version: 1` / `is_finished` / `next_start_time` / `next_duration_minutes` / `mode`），
   因此**同一段续读循环在两条通道之间可以无感切换**，只需换工具名；
   注意 `mode` 是切片模式（`oneshot` / `chunked`），本次任务预设看 `task` 字段；
   若返回里带 `clamped: true`，说明请求的时长被载荷上限收窄，按 `OMNI_STATUS` 的续读参数接着读；
3. **事实依据**：以代读回来的逐字稿为音频事实来源；**不得**把它当成摘要就跳过细节——
   需要完整讲授内容时逐片读全，需要板书/例题细节时用 `instruction` 追加要求。

**通道无关的后续步骤（两条通道都照此收尾）**：

- **教材编写**：
  - 载入任务书内所选类型的文章撰写提示词（当前提供 `learning` 学习版与 `legacy` 旧版）；
  - 结合**真正处理过**的案例、例题、板书比喻因材施教撰写长文，严禁脱离音频凭空脑补；
  - 讲师只在幻灯片上展示、音频里没有逐字念出的代码或表格**不要替他补写**，更不要基于补写出来的内容做逐行解析；
- **落盘保存**：用**宿主的文件写入能力**创建 `<产物根>/<task>/articles/PXX_*_精读文章.md`（≥ 1000 字节方视为完成）。

### 4.3 派发与回报协议（与阶段二 § 5.4 同构）

阶段一按**执行者纪律**（§ 1 红线 5）推进：主 Agent 只做调度与验收，听音与写作交给子智能体。

| 环节 | 做法 |
| :--- | :--- |
| 取载荷 | 派发前**必须**跑工具取载荷，**禁止手抄路径**（手抄会导致同一集被派两次，白烧 35~90k token）。两侧各一个入口：**转录侧** `queue_tracker.py --next-transcribe N --json`（块音频 / 块内时间表 / 逐字稿目标路径）；**写作侧** `queue_tracker.py --next-article N --log-dispatch --json`（只返回逐字稿已就绪且长文缺失的集，载荷含每集 `task_file` / `transcript_file` / `target_article`）。回退链路用 `--next N` |
| 派发粒度 | 默认 **一集一子智能体**；当「集数 ≥ 15 且单集预算 ≤ 40k token」时按 `suggest_batch` 建议改为 **3~5 集/子智能体** |
| 并发 | 建议 5~6（`suggest_workers` 给出建议值；不得超过宿主并发上限） |
| 子智能体输入 | **直接转交该集任务书**（`articles/PXX_*_TASK.md`）——它已含完整撰写提示词与红线，派发词不必也不得重述规范；**主 Agent 不代读、不代听** |
| 子智能体输出 | 只写 `articles/PXX_*_精读文章.md`，**不回传正文**（正文回传会把主上下文重新撑满） |
| 回报格式 | 固定一行：`P07 | 文件路径 | 字节数 | 执行者`（打包派发写成 `P07-P11 | …`） |
| 验收 | `queue_tracker.py --summary` 看 `STAGE1_DONE`；`--next N` 复核剩余待办 |
| 返修 | 质检不达标时，把「文件:行号:原文」贴回该集（或该包）子智能体重派，最多 2 轮；仍不达标由主 Agent 亲自返修该集 |

> **派发台账（观察性证据）**：加 `--log-dispatch` 会把本次建议的分集追加写入 `<task>/.dispatch_log.jsonl`。
> 它记录的是「工具建议派发了哪些集」，**不等于**「谁真的写了」——执行者身份无法在工具层验证；
> 台账的用途是事后复盘派发节奏（例如某工作区从未出现台账，说明阶段一没有走派发流程）。

### 4.4 阶段验收

```bash
# —— 转录侧 ——
python scripts/queue_tracker.py --next-transcribe 2 --json   # 取待转录的块（含块音频/时间表/逐字稿目标）
python src/cli.py split-transcript "<工作区目录>" [--block N] # 按块内时间表切出分集逐字稿（幂等）

# —— 写作侧 ——
python scripts/queue_tracker.py --next-article 5 --json --log-dispatch  # 只取「逐字稿已就绪且长文缺失」的集
python scripts/queue_tracker.py --next 5 --json --log-dispatch   # 不过滤逐字稿（回退链路/排查用）
python scripts/queue_tracker.py --summary    # 单行状态：TOTAL/DONE/PENDING/STAGE1_DONE + 块级转录进度
python scripts/queue_tracker.py --pattern "<目录名关键字>"   # 多课程并存时指定工作区（否则取最近活动的那个）

# —— 放行门禁（全部长文完成后，合格才派发后续模块任务）——
python scripts/article_grounding_check.py --strict   # 长文是否真的基于本集逐字稿（实体覆盖率）
python scripts/note_quality_check.py --strict        # 笔记成色（阶段二用）
python scripts/render_compat_check.py --strict       # 渲染合规
```

- `STAGE1_DONE=1`（全部分集长文均 ≥ 1000 字节）是**硬前提**；
- 块级转录进度看 `--summary` 的 `BLOCKS/BLOCKS_TRANSCRIBED/TRANSCRIPT_READY` 三项。
  **它们是独立信号，不参与 `STAGE1_DONE`**：老工作区（听音链路、无块清单）三项为 0，
  不能因此判它未完工；
- 全部门禁通过后才进入阶段二。

### 4.5 门禁 vs 纪律（边界声明，不要把纪律当成机器门禁）

| 项 | 性质 | 工具层能否校验 |
| :--- | :--- | :--- |
| 长文 ≥ 1000 字节、任务书存在、逐字稿/切片清单齐备 | **机器门禁** | ✅ 可校验（`queue_tracker` / `sync` / `note_quality_check`） |
| 长文风格命中已提供预设（`learning` / `legacy`） | **机器门禁** | ✅ 未命中即 `exit 4` |
| **长文是否基于本集逐字稿**（红线 2 的默认链路） | **机器门禁（启发式）** | ✅ `article_grounding_check.py`：逐字稿技术实体（英文标识符 + 多位数字）在长文里的覆盖率；低于下限报警，`--strict` 时非零退出。**它是启发式**：只测技术实体，中文表述为主但忠实于逐字稿的长文也会偏低；无逐字稿的集不参与判定 |
| **谁写的**（主 Agent 还是子智能体） | **纪律条款** | ❌ 不可校验（只能靠 `.dispatch_log.jsonl` 观察派发节奏） |
| **是否真的听过音频**（仅 `--no-merge` 回退链路） | **纪律条款** | ❌ 不可校验（只能要求正文含音频里的真实案例/例题） |
| **转录是否忠于原声**（块级转录链路） | **纪律条款** | ❌ 不可校验（ASR 质量由外部模型决定；写作侧只能照逐字稿写，不替它补听） |
| 转录角色是否只有 2 个、写作是否按块派发 | **纪律条款** | ❌ 不可校验 |
| 并发与打包是否按建议执行 | **纪律条款** | ❌ 不可校验 |

> **一句话**：`article_grounding_check.py` 把「文章用了语料没有」变成了可量化的门禁，但它只能
> 证明「用了」，不能证明「用得对」——取舍是否恰当、有没有过度展开，仍需抽样复核。

---

## 5. 阶段二：两趟语义聚合（模块 → 笔记，缺规划不停机）

阶段二由工具链与 Agent 交替推进，**分两趟**：第一趟把工作区集号切成**知识模块**（供教材），
第二趟把模块**归并成若干篇笔记**（供笔记）。每一趟都是「Agent 出规划 → 工具按规划派发任务书」。

```bash
python src/cli.py cluster-notes "<链接或路径>"
```

> **两趟缺规划都不停机**：工具会导出规划任务书、用兜底粒度继续把流程走完，命令**始终正常退出**；
> 盘上的 `topic_plan.json` / `note_plan.json` **一个字节都不会被兜底结果覆盖**，
> Agent 补齐规划后重跑即自动替换。凡是「越界／缺失／重复」的规划都当场抢救（裁剪、补齐、先到先得），
> 不会像旧版那样把整个阶段二卡在 `exit 2` 上。

### 5.1 第一趟：模块规划（TOPIC_PLAN_TASK）

- **任务书**：`<产物根>/<task>/topic_plan_TASK.md`
- **目标文件**：`<产物根>/<task>/topic_plan.json`
- **规划依据**：各集**长文标题**（Agent 读完语料后写的 H1，如「变量与作用域」），分集原名只作括号参考——
  分集标题是 UP 主写的一句话索引，长文标题才反映这一集真正讲了什么，照分集标题切容易切错。

Agent 需读取任务书中的规划提示词，产出 JSON Array 并写入目标文件。硬性约束：

1. 分集必须**恰好覆盖 `parts.json` 的全部实际集号**（如 `--range 9-87` 的工作区就是 P09–P87，共 79 集）：
   不得遗漏、重复或越界；集号不连续**仍然通过**，只提示缺口；
   **严禁把集号重排成 1..N**——集号是工作区的事实，工具与 Agent 都无权改写它。
2. **知识块的大小完全由知识体系的语义边界决定，不设集数上限**：2 集可以是一块，20 集也可以是一块；
   判据只有一个——换了一个独立的大主题才另起一块。**宁大勿碎**：宁可几集合成一个厚实的模块，
   也不要把成体系的一章切成零碎小块。
3. `block_title` 必须由长文主题提炼，不得照抄分集标题。

规划**不完美也不必推倒重来**：越界集号被裁掉、被重复认领的集号先到先得、没人认领的集号补成占位块，
流程照常走完并把做了什么打印出来。只有「一个块都留不下来」时才退回**顺序占位切分**（输出会明确标注
「占位」）；此时工具**不派发笔记任务书**——占位块没有语义边界，照它写出来的笔记会内容错位，
比停下来更糟。

### 5.2 第二趟：笔记归并（NOTE_PLAN_TASK）

第一趟就绪后重跑同一命令，工具读各模块及其**各集长文标题**，导出归并任务书：

- **任务书**：`<产物根>/<task>/note_plan_TASK.md`
- **目标文件**：`<产物根>/<task>/note_plan.json`，形如
  `[{"note_id": 1, "note_title": "进程管理与调度体系", "blocks": [3, 4, 5], "core_theme": "…"}]`
- **约束**：`blocks` **恰好覆盖全部模块各一次**（不得遗漏、不得重复）；集号由 `blocks` 自动推导，
  **不需要 Agent 填写**。

**一篇笔记可以跨多个模块**（这是常态）：一个模块整体进一篇笔记、不拆开，但讲同一套体系、同一条技术栈、
同一条学习路线的若干模块**必须合并**成一篇。宗旨是**宁可少而厚，不要多而碎**——几十篇两页纸的小笔记
摊开来，读者根本串不成体系。归并失位（漏认领／重复认领／引用不存在的模块）同样当场抢救后继续。

### 5.3 第三步：笔记任务书派发（NOTE_TASK，文章直供）

两趟规划就绪后重跑，工具逐篇校验**该篇涵盖各集的长文是否齐备**，齐备即导出笔记任务书：

- **任务书**：`<产物根>/<task>/notes/笔记XX_<主题>_TASK.md`
- **目标文件**：`<产物根>/<task>/notes/笔记XX_<主题>_笔记.md`
- **语料**：该篇涵盖各集 `articles/PXX_*_精读文章.md`（任务书中列出**路径清单 + 字节数**）
- **抬头会写明「涵盖模块」与「涵盖分集」**，让子智能体清楚这是一篇**跨模块聚合**笔记，
  而不是「一个模块一篇」

Agent 需按任务书内的 `MODULE_NOTE_PROMPT`（专属提示词）撰写，逐条落实其中的六节：
**笔记信息与唯一事实来源**、**工作流程与目标形态**、**内容要求**、**结构要求**、
**条目骨架**、**排版**；笔记只有这一种风格，无需再指定 `--style`。
若某篇尚有分集没有长文，该篇会被跳过并打印待办，其余照常推进。

> [!IMPORTANT]
> **语料纪律（v1.7 起）**：笔记的唯一事实来源是**单集精读长文**；知识元（kernel）已从「前置门禁」降级为「可选索引」——默认不参与，只有显式追加 `--kernel-index` 时才会把历史知识元作为定位索引注入。原因是空壳知识元会把笔记质量一并拖垮。

> **笔记产物命名**：现行规范名是 `笔记XX_<主题>_笔记.md`；成品后缀若不是规范名，仍会按
> `笔记XX_*` 前缀回退认出（避免同一篇被重复派发）。旧命名 `模块XX_*` 已不再兼容：它属于
> 「一模块一篇」时代的粒度，与归并后的笔记不是一回事，按编号硬认会把别人的成品算成自己的。

> **被取代的任务书会自动作废**：归并粒度变了（例如从「一个模块一篇」变成「一篇装 5 个模块」）之后，
> 上一轮留下的、编号或主题已对不上的 `笔记XX_*_TASK.md` 会被自动清掉并打印数量——否则主 Agent 会照着
> 它们再派一批**内容已经错位**的笔记。已有成品落盘的任务书不动（那是交付记录，交给 `cleanup`）；
> 用 `--block-id` / `--start-block` / `--end-block` **分批派发**时也不会清（那时整批待办大部分都还有效）。

### 5.4 子智能体派发规范（推荐做法）

笔记撰写工作量大且各篇彼此独立，**推荐由主 Agent 派子智能体并行产出**（**一篇笔记一个子智能体**）：

| 环节 | 做法 |
| :--- | :--- |
| 派发粒度 | **一篇笔记 = 一个子智能体**，互不交叉，避免上下文互相污染 |
| 输入 | **直接转交该篇任务书**（`notes/笔记XX_*_TASK.md`）——它已含专属性提示词与版式规范，派发词不必也不得重述；子智能体再按清单**逐篇整篇读完**该篇涵盖的全部 `articles/`（主 Agent 不代读） |
| 输出 | 子智能体只写 `notes/笔记XX_*_笔记.md`，**不回传正文**；回报固定一行：`笔记XX \| 文件路径 \| 字节数 \| 覆盖分集` |
| 并发 | 建议 5~6 个并发；笔记多时分批派发 |
| 返修 | 质检不达标时，把质检脚本输出的「文件:行号:原文」贴给该篇子智能体重派，最多 2 轮；仍不达标则由主 Agent 亲自返修该篇 |

> 子智能体**不必自报**「套话 0／分集标题 0／断句 0」——那是 `note_quality_check.py` 的活（§ 5.7），
> 让子智能体自己判等于让它自证合格，没有意义。汇报回到「覆盖集号 + 字节数」两件事即可。

### 5.5 模块合辑教材（无需语义往返）

```bash
python src/cli.py cluster-articles "<链接或路径>"
```

直接以 `articles/` 为输入、按**第一趟的模块边界**整编为 `textbooks/模块XX_<主题>_精读全书.md`，
自动将单章标题降级并补充承前启后段落。`articles/` 完整保留，不做任何删除。

> 教材按**模块**分册，笔记按**归并后的笔记**分篇——两者粒度不同是刻意的：教材要覆盖全、便于通读，
> 笔记要成体系、便于检索。模块规划缺失时，教材退回按分集标题的章节号/讲次分组（`综合模块` 兜底）。

> **教材分册受语料体积约束（300KB）做归一，笔记则完全不做体积切分。** 实测（同一模块 23 篇
> 长文 / 310KB 对拍）：把一篇笔记按体积切成两篇，知识点覆盖没有变好（96.1% → 94.7%），
> 却多出 39% 的体积与 22 处跨篇重复——切点由字节数决定，切出的两半互不知道对方写了什么；
> 还会让笔记编号与模块映射错位。因此**一篇笔记与 `note_plan.json` 的一条严格一一对应**，
> 模块语料再大也不拆。

> **教材标题不写序号**：章标题直接写主题名（不再写「第 N 章：」），目录改用有序列表
> （`1. 主题名`，序号由渲染器生成），长文标题继承进来时先幂等去号再整体降级。判定与去号规则
> 收敛在 `src/core/heading_numbers.py` 一处，门禁（`render_compat_check.py --require-no-numbering`）
> 与批量清理脚本共用同一份规则，避免两边判得不一样。

### 5.6 任务书与目标文件对照表

| 环节 | 任务书（Agent 读取） | 目标文件（Agent 写入） | 复用/放行条件 |
| :--- | :--- | :--- | :--- |
| 阶段一 单集长文 | `articles/PXX_*_TASK.md` | `articles/PXX_*_精读文章.md` | 文件 ≥ 1000 字节 |
| 阶段二① 模块规划 | `topic_plan_TASK.md` | `topic_plan.json` | 实际集号全覆盖且唯一（不完全合法则抢救后继续） |
| 阶段二② 笔记归并 | `note_plan_TASK.md` | `note_plan.json` | 全部模块各被认领一次（不完全合法则抢救后继续） |
| 阶段二③ 笔记任务书 | `notes/笔记XX_*_TASK.md` | `notes/笔记XX_*_笔记.md` | 该篇涵盖各集长文齐备即导出 |
| 阶段二④ 模块教材 | ——（纯工具整编） | `textbooks/模块XX_*_精读全书.md` | `articles/` 齐备即整编 |

> [!TIP]
> 所有任务书均为「读完即写盘」模式：工具链只负责准备语料、渲染提示词与校验产物，**真正的语义工作全部由宿主 Agent（通常为子智能体）完成**。

### 5.7 交付前质检与收尾（机器门禁）

```bash
python scripts/note_quality_check.py --strict    # 笔记成色：套话 / 空壳标题 / 分集标题 / 行内残缺引用 / 分集口吻 / 断句 / 结构缺件
python scripts/render_compat_check.py --strict   # 渲染合规：GitHub 告警块 / 围栏外字符画 / 围栏配对 / 语言标识
python src/cli.py cleanup --dry-run              # 任务书回收预演（成品产出后才回收，每类留 1 份范本）
python src/cli.py sync                           # 按磁盘对账回填 manifest.json
```

- **笔记成色致命项**（`套话填充 / 空壳标题 / 分集平铺标题 / 行内残缺引用 / 分集口吻`）在两套合格语料上实测均为 0，必须清零；
- **断句与结构缺件**为启发式警告项：默认按「每份 ≤ 4 处断句」提示，**结构缺件默认只报告不拦**，
  需要纳入门禁时显式加 `--require-structure`；
- **渲染致命项**为 `GitHub 告警块 / 围栏外裸字符画 / 围栏配对`；**围栏缺语言标识默认只提示不拦**，
  需要死守时加 `--require-lang`；
- 任务书是**临时派发物**：成品产出后由 `cleanup` 回收，每个类别保留编号最小的 1 份作为提示词范本；`topic_plan_TASK.md` / `note_plan_TASK.md` 属课程级规划任务书，永不回收。

> **这两个脚本不在流水线上拦人**：它们是交付前由主 Agent **手动**跑的体检，只有 `--strict` 的致命项才返回非零退出码。平时跑 `cluster-notes` / `cluster-articles` / `pipeline` 都不会被它们挡住。

---

## 6. CLI 常用命令速查表

所有任务必须通过以下标准入口调用（功能一致，三选一均可）：
- 仓库推荐：`python src/cli.py <子命令>`
- 免安装脚本：`python scripts/run.py <子命令>`
- 系统命令：`video2book <子命令>`

```bash
# 1. 解析合集结构与时长
python src/cli.py parse "<链接或本地目录>" [--json]

# 2. 执行音频下载流水线（准备阶段 + 导出两类任务书：收音频 → 装箱成块 → 块级转录任务书 + 单集长文任务书）
#    注意：--article-type 是长文提示词风格，**必填**；learning=学习（推荐）/ legacy=旧版
#    不传则打印风格菜单并当场询问，确认不了即 exit 4 终止
python src/cli.py pipeline "<链接或本地路径>" --all --article-type learning
python src/cli.py pipeline "<链接或本地路径>" --range 1-10 --article-type legacy
python src/cli.py pipeline "<链接或本地路径>" --all --article-type learning --block-minutes 45  # 块时长目标（默认 60）
python src/cli.py pipeline "<链接或本地路径>" --all --article-type learning --no-merge            # 回退「逐集听音」老链路

# 2b. 块级转录链路的两个离线入口（幂等，可反复重跑）
python src/cli.py merge-audio "<工作区目录>" [--block-minutes 45] [--force]  # 只重跑装箱合并 + 重出块级转录任务书
python src/cli.py split-transcript "<工作区目录>" [--block 1]                # 块级逐字稿 → subtitles/PXX_*_逐字稿.md

# 3. 单集文章任务书（单集直出长文；已存在长文则跳过）
python src/cli.py transcribe "<链接或本地路径>" --page 1 --article-type learning

# 4. 音频指纹去重（自动复用相同分集的语料与长文，0 Token 消耗）
python src/cli.py dedup "<链接或本地路径>"

# 5. 动态任务队列追踪器（待办分集 + 阶段门禁 + 派发建议/载荷/台账 + 块级转录进度）
python scripts/queue_tracker.py --next 5                          # 待办分集与目标路径
python scripts/queue_tracker.py --next 5 --json --log-dispatch     # 派发载荷（转交子智能体）+ 写派发台账
python scripts/queue_tracker.py --next-transcribe 2 --json         # 转录侧：取待转录的块（块音频/时间表/逐字稿目标）
python scripts/queue_tracker.py --next-article 5 --json --log-dispatch  # 写作侧：只取「逐字稿已就绪且长文缺失」的集
python scripts/queue_tracker.py --summary                         # 单行状态 + SUGGEST_WORKERS/BATCH + 转录进度

# 6. 阶段二：整编模块教材全书（按第一趟模块分册，输出至 textbooks/，原有 articles/ 完整保留）
python src/cli.py cluster-articles "<链接或本地路径>"                             # 已有教材默认复用
python src/cli.py cluster-articles "<链接或本地路径>" --force                     # 按最新章节强制重编

# 7. 阶段二：复习笔记（只有一种版式）
#    注意：两趟规划（模块 ➔ 归并成笔记）都由 Agent 产出；缺规划不会卡住，命令始终正常退出。
#    笔记建议由子智能体按「一篇笔记一个子智能体」并行产出（见 § 5.4）。
python src/cli.py cluster-notes "<链接或本地路径>"                                # 笔记只有一种风格，无需 --style
python src/cli.py cluster-notes "<链接或本地路径>" --force                        # 强制重导全部笔记任务书
python src/cli.py cluster-notes "<链接或本地路径>" --force-plan                   # 强制重出两趟规划任务书（忽略盘上旧规划）
python src/cli.py cluster-notes "<链接或本地路径>" --kernel-index                 # 可选：注入历史知识元作索引
python src/cli.py cluster-notes "<链接或本地路径>" --block-id 3                   # 只派发第 3 篇笔记（--start-block/--end-block 同理）

# 8. 交付前质检与收尾（手动体检，不在流水线上拦人）
python scripts/article_grounding_check.py --strict  # 长文是否真的基于本集逐字稿（实体覆盖率，启发式）
python scripts/note_quality_check.py --strict      # 笔记成色体检（套话/空壳标题/分集标题/断句/结构缺件；含默认不拦的提示项）
python scripts/render_compat_check.py --strict     # 渲染合规体检（告警块/裸字符画/围栏配对；含默认不拦的提示项）
python src/cli.py cleanup --dry-run                # 任务书回收预演（成品产出后才回收，每类留 1 份范本）
python src/cli.py sync                             # 按磁盘对账回填 manifest.json

# 9. 环境与工具链自检
python scripts/selfcheck.py
python src/cli.py info

# 10. 登录凭证：持久化保存 SESSDATA（保存一次，后续命令免传）
python src/cli.py login --sessdata "<SESSDATA>"
python src/cli.py logout
```

> 阶段一的音频处理依赖 MCP 工具：**有原生音频模态的宿主**用 `omni-media:read_audio`（零凭证，服务本体在配套仓库的 `mcp/`），
> **没有原生音频模态的宿主**用 `omni-media-ext:read_media`（服务本体在配套仓库的 `mcp-ext/`，由配置文件指定的外部模型代读）。
> 两个服务同属仓库 [LINJIANG12/omni-media](https://github.com/LINJIANG12/omni-media)。
> 两者各自独立成包、**互不 import**，与技能无运行时依赖，装一次即可长期使用；**分页契约同构**（同一 `OMNI_STATUS` 注释、`contract_version: 1` 与续读循环），
> 切换只需换工具名。选择规则见 §4.2，接入方式见 `references/install.md`。

### 6.1 完整参数表（速查表之外的开关都在这里）

| 入口 | 参数 | 用途 |
| :--- | :--- | :--- |
| `parse` | `--limit N` / `--json` | 列表最多显示 N 条（默认 10）/ 输出 JSON |
| `audio` | `--page N` `--all` `--range X-Y` `--quality low\|medium\|high` `--url-only` `--output DIR` `--chunk-minutes N` `--json` `--force` | 单集或批量取音频；`--url-only` 只打印直链不下载；`--chunk-minutes` 默认 10；`--output` 覆盖音频目录 |
| `transcribe` | `--page N` `--output PATH` `--article-type <风格>` | 单集文章任务书；`--output` 仅在长文已存在时用于导出副本 |
| `pipeline` | `--all` `--range X-Y` `--page N` `--quality <档>` `--prefetch-workers N` `--skip-failed` `--chunk-minutes N`（默认 60） `--block-minutes N` `--no-merge` `--force` `--article-type <风格>` `--task NAME` `--base-dir DIR` | 阶段一主入口；`--skip-failed` 把音频失败集记入跳过名单继续跑；`--block-minutes` 是**块时长目标**（默认取 `BVB_AUDIO_BLOCK_MINUTES`，再默认 60；硬上限看 `BVB_AUDIO_ONESHOT_LIMIT_MINUTES`）；`--no-merge` 回退「逐集听音」链路；`--force` 重派已完成分集 |
| `merge-audio` | `<工作区目录>` `--block-minutes N` `--force` | 单独重跑音频装箱合并并重出块级转录任务书（幂等；`--force` 忽略指纹重建块） |
| `split-transcript` | `<工作区目录>` `--block N` | 把块级逐字稿按块内时间表切成 `subtitles/PXX_*_逐字稿.md`（幂等；`--block` 只处理指定块） |
| `cluster-notes` | `--force` `--force-plan` `--kernel-index` `--block-id N` `--start-block N` `--end-block N` | 两趟语义聚合；后三个按**笔记序号**只处理指定区间（参数名是历史遗留）；`--force` 强制重导笔记任务书 |
| `cluster-articles` | `--force` | 默认复用已有教材，`--force` 按最新章节重编 |
| `dedup` | `--dry-run` | 只报告重复分集，不复制语料与长文 |
| `cleanup` | `--keep N`（默认 1） `--dry-run` `--task 关键字` `--all` | 每类保留 N 份任务书范本；`--all` 为兼容保留（不加即全量） |
| `sync` | `--dry-run` `--task 关键字` `--all` | 按磁盘对账回填 manifest |
| `note_quality_check.py` | `--strict` `--require-structure` `--max-truncated N`（默认 4） `--dir` `--task` `--base-dir` `--json` | 结构缺件默认只提示，`--require-structure` 才纳入门禁 |
| `render_compat_check.py` | `--strict` `--require-lang` `--dir` `--task` `--base-dir` `--json` | 语言标识默认只提示，`--require-lang` 才纳入门禁 |
| `queue_tracker.py` | `--next N` `--next-article N` `--next-transcribe N` `--summary` `--json` `--dir PATH` `--pattern 关键字` `--base-dir DIR` `--log-dispatch` | 派发前取载荷，三个入口互斥：`--next-transcribe N`（转录侧：待转录的块）、`--next-article N`（写作侧：只返回逐字稿已就绪且长文缺失的集）、`--next N`（不过滤，回退链路/排查用）；`--summary` 额外给出 `BLOCKS/BLOCKS_TRANSCRIBED/TRANSCRIPT_READY` 转录进度；多课程并存时必须用 `--dir`/`--pattern`；`--log-dispatch` 追加派发台账（默认关闭） |
| `article_grounding_check.py` | `--strict` `--min-freq N`（默认 2） `--min-coverage F`（默认 0.5） `--dir` `--task` `--base-dir` `--json` | 长文依据级校验：逐字稿技术实体在长文里的覆盖率，低于下限报警（默认提示级，`--strict` 才纳入门禁）；无逐字稿的集不参与判定 |
| `cleanup_tasks.py` | `--keep N` `--dry-run` `--task` `--json` `--strict` | `cleanup` 的独立脚本入口（功能一致） |

---

## 7. 交付产物与格式标准

完成处理后，系统输出三类结构化资产：

| 产物 | 路径（相对产物根） | 说明 |
| :--- | :--- | :--- |
| **单集教材长文** | `<产物根>/<task>/articles/PXX_*_精读文章.md` | 每集独立长文，按该集所选**长文风格**的提示词撰写（`learning` 学习＝保住讲师讲课风格 + 高信息密度 + 成稿好读好看；`legacy` 旧版＝客观学术第一视角 + 随堂自测），含真实教学案例与讲师亲口讲的推导。模块整编后**严格保留，不予删除** |
| **复习笔记** | `<产物根>/<task>/notes/笔记XX_*_笔记.md` | 按**第二趟归并后的笔记**分篇（一篇可跨多个知识模块），按任务书内的 `MODULE_NOTE_PROMPT` 产出：只有 H1 + 按知识主题分节的条目（**不写抬头元信息、知识拓扑树、节级主旨句、来源标注**），标题最多到 `####` 且**不得手写序号**（阅读器会自动编号，手写会叠字）。**笔记只有这一种风格**（旧版八种风格矩阵已删除），无需 `--style`；原生支持 Markmap / XMind 导入 |
| **模块合辑教材** | `<产物根>/<task>/textbooks/模块XX_*_精读全书.md` | 按**第一趟的模块**分册编排的完整合辑教材，含全景导读与章节逻辑过渡。章标题与目录**不写序号**（序号交给渲染器），继承自长文的手写序号在整编时被幂等剥掉 |

> **笔记与教材的分册粒度不同，这是有意的**：教材按知识**模块**分册（覆盖全、便于通读），
> 笔记按**归并后的笔记**分篇（成体系、便于检索）。一个模块整体只进一篇笔记，一篇笔记可以装多个模块。

### 7.1 工作区目录结构

以下路径**均相对产物根**（默认 `<你的工作目录>/output/`；在容器内工作时为 `<容器根>/output/`）：

```text
<产物根>/<task>/
├── audio/                     # 提取的音频与自动切片
│   └── _blocks/               #   块级转录的块音频与清单（BLK01_P08-P12.m4a + blocks.json）
├── parts.json                 # 分集拓扑缓存（**集号基准**：接口受阻时离线自愈依赖它；局部运行按 page 合并，不会截断）
├── manifest.json              # 任务清单与断点续跑状态（可用 `cli.py sync` 按磁盘对账回填）
├── topic_plan.json            # ① 模块规划（Agent 产出；消费方＝教材）
├── topic_plan_TASK.md         # ① 规划任务书（课程级唯一，永不回收）
├── note_plan.json             # ② 笔记归并（Agent 产出；消费方＝笔记）
├── note_plan_TASK.md          # ② 归并任务书（课程级唯一，永不回收）
├── articles/                  # 单集长文 + 派发任务书
│   ├── PXX_*_TASK.md          #   单集长文任务书（临时派发物，完成后回收，保留 P01 一份范本）
│   └── PXX_*_精读文章.md       #   单集长文（最终产物，严格保留）
├── subtitles/                 # 逐字稿与人工语料的**正式**存放位置
│   ├── BLKxx_Paa-Pbb_转录任务书.md  #   块级转录任务书（临时派发物）
│   ├── BLKxx_Paa-Pbb_逐字稿.md      #   块级原始逐字稿（转录角色的产出，可溯源）
│   ├── PXX_<标题>_逐字稿.md         #   分集逐字稿（写作角色的唯一事实依据，由 split-transcript 切出）
│   ├── PXX_<标题>_clean.txt         #   人工清洗稿 / 旧链路逐集文本（**有则优先复用**，不重复转录）
│   └── kernels/               #   知识元（可选索引，历史工作区遗留，默认不参与笔记生成）
├── notes/                     # ③ 笔记 + 派发任务书（每类保留 1 份任务书范本）
│   ├── 笔记XX_*_TASK.md       #   笔记任务书（临时派发物，成品产出后回收）
│   └── 笔记XX_*_笔记.md       #   笔记成品（跨模块聚合，阶段二产物）
└── textbooks/                 # ④ 模块合辑教材
```

产物根同时存放运行时状态文件：`.sessdata.json`（凭证，`cli.py login`）、`.wbi_keys.json`（WBI 签名密钥缓存）、
`.cli_status.json`（上次 412/熔断记录）。这些文件**永远不在代码仓库里**，因此不会被误提交。

> **处理范围**：流水线处理的是**当前稿件被选中的那批分 P**（即 `parts.json` 的内容）——`--range 9-87` 得到的就是 9..87 共 79 集，集号保持原样、不重排。
> `cluster-notes` / `cluster-articles` 的集号基准**一律取工作区 `parts.json`**，不用在线解析出来的课程全集
> （在线解析只用于首次建工作区）；课程标题同理，取 manifest → 工作区目录名，**离线也能跑**。
> **B 站独立 BV 合集已原生支持**：`space.bilibili.com/<mid>/lists/<season_id>?type=season`、
> `www.bilibili.com/list/<mid>?sid=<season_id>`、合集内任意单集视频链接，以及显式
> `season:<id>` 都会归一到同一工作区；合集 episodes 会成为 P01..PN，下载时逐集使用自己的
> `bvid + cid`。因此 `pipeline --all` / `cluster-*` 可以跨越独立 BV 完整处理整门课。

> **工作区名的推导与找回**：工作区名是「清洗后的课程标题 + `_<BV号>`」，标题过长时**先给 BV 号留位再截标题**，
> 因此新工作区的名字里总是带着完整 BV 号，命令能稳定找回它。更早建立的工作区可能把 BV 号一起截掉了
> （实测：某个 80 字符目录名里根本没有 BV 号），这类目录**无法**由 BV 号自动找回——
> 接口受阻需要离线自愈、或命令落到了别的目录时，请显式加 `--task "<工作区目录名>"`（那条路是确定的）。
> 工具刻意不在这种情形下猜：猜错会静默读写到别门课的产物上。

> **产物命名兼容**：工具层的规范名是 `PXX_*_精读文章.md`，但复用判定走**宽容定位**
> （`KernelExtractor.find_article`）：历史工作区里形如 `PXX_<标题>.md` 的无后缀长文同样被认作已完成，
> 不会被要求重写。任务书（`*_TASK.md`）永远排除在产物判定之外。
> 笔记同理：现行规范名是 `笔记XX_*_笔记.md`，成品后缀不是规范名时按 `笔记XX_*` 前缀回退复用；
> 旧命名 `模块XX_*` 不再兼容（见 § 5.3）。

> **任务书回收**：`*_TASK.md` 是工具层写给 Agent 的**临时派发物**，成品产出后由 `cli.py cleanup`
> （或 pipeline 收尾）自动回收，**每个类别保留编号最小的 1 份**作为提示词范本，便于随时翻阅写法。
> 成品尚未产出的任务书一律保留，不会误删进行中的派发。

### 7.2 断点续跑

工具链以磁盘产物为唯一进度依据，重跑同一条命令即可续作：

- 单集长文、模块规划、笔记归并、笔记、模块教材均按 § 5.6 表格的条件自动复用（笔记与模块教材命中成品即跳过，打印 `[cached]`）；
- 删除产物根下某个**产物**文件（如 `<产物根>/<task>/articles/PXX_*_精读文章.md`），即视为重新派发该环节；
- 任务书会被自动回收，因此**不要靠删除任务书来重派**。需要重导时的正确做法：
  - 笔记：`cluster-notes --force`；
  - 两趟规划：`cluster-notes --force-plan`（重出 `topic_plan_TASK.md` / `note_plan_TASK.md` 并忽略盘上规划）；
  - 模块教材：`cluster-articles --force`；
  - 单集长文：`pipeline --force`（`transcribe` 没有 `--force`，重派请直接删除该集长文后再跑）；
- 用 `python scripts/queue_tracker.py --next 5` 查看阶段一待办队列，`--summary` 获取单行状态；多课程并存时加 `--pattern` / `--dir` / `--base-dir`；
- 若 `manifest.json` 与实际产物不一致（例如 Agent 直接写盘后清单未回填），执行 `python src/cli.py sync` 按磁盘对账。

> **规划卡住时怎么办**：`topic_plan.json` / `note_plan.json` 不合法**不会**让命令失败退出——工具会当场
> 裁剪越界块、补齐没人认领的集号、按模块兜底归并，并把做了什么打印出来，命令正常结束。修好规划文件后
> 重跑一次即可换成真规划；盘上那份**永远不会被工具改写**。

### 7.3 阅读器与渲染兼容

三类交付物默认在 **Typora** 中阅读，同时兼容 VS Code Markmap 与 XMind 导入。

- `render_compat_check.py --strict` 只把「告警块 / 围栏外裸字符画 / 围栏配对」当致命项；
  **缺语言标识默认只统计不拦**（历史成品存在既有缺口），需要死守时加 `--require-lang`；
- 行内公式 `$…$` 需在 **Typora → 偏好设置 → Markdown → 勾选「内联公式」** 后才会正常渲染（这是阅读侧的一次性设置，请向使用者说明）。

> **范围说明**：本规范文档自身出现的告警块（`> [!IMPORTANT]` 等）仅用于提示阅读本文档的人与 Agent；**交付产物一律禁用该语法**，两者不可混淆。

产物体系总览、长文类型矩阵与拓扑树样例请查阅：[references/delivery_matrix.md](references/delivery_matrix.md)。

非视频作品（抖音图文/图集 note：无口播、不参与长文生成）的识别口径与处置规则见：
[references/non-video-works.md](references/non-video-works.md)。

---

## 8. 环境要求与缺失处理

本 Skill 的运行依赖**三层**：Python 解释器、系统 ffmpeg、宿主的多模态听音工具。任一层缺失时按本节口径处理——**能降级的降级，不能降级的明确报错并给出可照做的下一步**，不要绕道，也不要用"编造内容"把流程假装跑通。

### 8.1 环境要求一览

| 依赖 | 必需性 | 缺失时会发生什么 |
| :--- | :--- | :--- |
| **Python 3.10+** | 必需 | 全部工具链命令无法启动（本仓库没有非 Python 实现） |
| **ffmpeg**（在 `PATH`） | 必需（取音频阶段） | `pipeline` / `audio` 在音频阶段失败，任务书不会落盘 |
| **ffprobe** | 可选 | 自动降级为 `ffmpeg -i` 解析时长（精度略低、速度略慢），流程照常 |
| **`read_audio` 或 `read_media`** | 必需（阶段一听音） | 阶段一取不到音频事实，必须停下提示用户挂载其一；**通道挂着但上游不可达**时同样停下，见 §8.2 ⑥ |
| **Python 3.12+** | 可选 | 仅影响链接去重的识别方式：3.12 以下没有 `Path.is_junction`，改用文件属性位识别重解析点，junction 与符号链接**同样被跳过**（不会重复计数） |
| **git** | 可选（仅自检用） | `scripts/selfcheck.py` 里两条「产物/凭证未入库」的校验降级为提示，其余断言照常 |

一条命令确认环境是否齐备：

```bash
python src/cli.py info        # Python / ffmpeg / ffprobe / 两条听音通道 / 三域路径
python scripts/selfcheck.py   # 全量契约自检（含 Python 3.10+ 语法兼容断言）
```

### 8.2 缺失情形与处理

**① 没有 Python（或版本低于 3.10）**
本工具链没有非 Python 的替代实现，**不要**改用别的语言重写，也不要手工拼装产物。先安装 **Python 3.10+** 并确认 `python --version` 可执行，再重跑；`info` 会打印当前解释器路径与版本，低于 **3.10** 时明确标红提示。

**② 没有 ffmpeg**
这是取音频与切片的**硬前置**，不是"可选增强"。`info` 会报出未找到并给出三平台安装命令（`winget install Gyan.FFmpeg` / `brew install ffmpeg` / `apt install ffmpeg`）；装好后要确保 `ffmpeg` 在 `PATH` 里，并**重开终端**再跑。

缺失时的表现分两种，**别把"没报错"当成"已就绪"**：

- **本地音视频**：`pipeline` / `audio` 在音频提取阶段直接失败，并给出可照做的提示（不再抛裸栈回溯）；
- **B 站远端下载**：不会报错，而是**退化为不转码**——把下载到的原始音频直接存成 `.m4a`（仍可播放，但体积与兼容性略差）。

因此请以 `info` 的 FFmpeg 检查结果为准，而不要以命令是否报错来判断。

**③ 两条听音通道都没挂（既无 `read_audio` 也无 `read_media`）**
阶段一**必须停下**并提示用户先挂载其一（配套仓库 `omni-media` 的 `mcp/` 或 `mcp-ext/`，装在哪都行；`info` 给出的目录只是默认位置的提示），**不得**跳过"真正处理过本集音频"这一步直接编造正文（见 §1 红线 2）。判断依据永远是**宿主自己的工具列表**，不要猜，也不需要为它配置任何路径。

**④ 没有外网 / B 站接口不可达**
`parse` / `pipeline` 对元数据接口做带退避的重试，并对 412 频控记录状态（`info` 的「上次 412/熔断状态」可查）。仍然失败时：补 `--sessdata` 后重跑，或改用本地音视频目录（本地任务不走网络）。**已建立的工作区可以离线继续**：`cluster-notes` / `cluster-articles` 会先尝试在线解析，失败后按工作区的 `parts.json` / `manifest.json` 离线自愈——集号与标题基准始终取自工作区，不依赖在线结果（见 §7.1）。

**⑤ 换位置部署（环境变量覆盖）**
`BVB_OUTPUT_DIR`（产物根）与 `BVB_HOME`（容器根）必须在**进程启动前**设置；`--base-dir` 可在命令行临时覆盖。
两者都不设时的默认行为是**产物落在当前工作目录下的 `output/`**（不需要任何配置；只有在容器内工作时才落到 `<容器根>/output`）；`info` 会标注每个路径的来源（环境变量 / 容器标记 / 工作目录）与容器标记是否存在（另见 §3 的运行前置契约）。

**⑥ 听音通道挂着、但它的上游不可达**
和 ③ 不是一回事：MCP 服务本身装好了、网关进程也在跑，但外部模型端点连不上它自己的上游，实际请求**全部**失败。典型形态是**网关自己可达**——`omni-media-ext status --probe` 只发 `GET {base_url}/models`，会报「可达」，而真正的转录/推理请求返回 5xx；实测一次事故：`/v1/models` 返回 200，但 token 获取 503（`Token acquisition timeout`），整条通道取不到任何逐字稿。
工具会在报错里点明「这条错误来自端点的**上游**」。此时**停止重试**（重跑不会变好），按提示检查本机代理/加速器是否在运行、能否连上上游，修好后重跑；**不要**去改 `/audio/transcriptions` 与 `model` 配置——那不是原因。

> **缺失处理总原则**：降级项（ffprobe→`ffmpeg -i`、3.12→仅识别符号链接、在线解析→工作区离线基准）静默降级并打印说明；硬依赖项（Python、ffmpeg、听音通道）缺失时**立即终止并给出下一步命令**，绝不静默跳过。
