# video2book — Agent 入口

本仓库是一个跨 agent 的 **Agent Skill**（把 B 站、YouTube、抖音网课/本地音视频重构为教材长文、模块全书与思维导图笔记）。

**完整说明以 [`CLAUDE.md`](CLAUDE.md) 为准**（本文件只是同一份内容的通用入口，便于各平台自动加载）。

快速事实：

- 技能定义：`skills/video2book/SKILL.md`（唯一真源）
- 工具链（技能的依赖，必须一起安装）：`skills/video2book/src/` 与 `skills/video2book/scripts/`
- **安装单元 = `skills/video2book/` 整个目录**（其余 README/平台声明/LICENSE 无需安装）
- 各平台安装对照：`skills/video2book/references/install.md`
- CLI 场景手册（八个场景）：`skills/video2book/references/cli-cookbook.md`
- 运行前置：Python 3.10+、系统 `ffmpeg`、以及配套仓库 [`omni-media`](https://github.com/LINJIANG12/omni-media) 提供的 `read_audio` 或 `read_media` 听音通道之一（B 站课程有中文字幕时可由字幕链路替代）
- 自检：`cd skills/video2book && python scripts/selfcheck.py`
- 容器根 `.git` 是 Codex 工作区标记，只允许保持为空；技能自检会拒绝含提交或 tracked 文件的根仓库。
- 跨多文件修改纪律：涉及多个文件时，直接在系统临时目录（`%TEMP%`）编写一次性脚本批量替换，验证通过后立即删除临时脚本，严禁逐文件逐处微调。

