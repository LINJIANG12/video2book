# Installing Video2Book for OpenCode

## Prerequisites

- [OpenCode](https://opencode.ai) installed
- Python 3.10+ and system `ffmpeg` on `PATH` (the Bilibili and local chains are pure standard library;
  the YouTube and Douyin chains also need the declared `yt-dlp` / `requests`, installed via `pip install -e .`;
  ffmpeg is a hard requirement for the audio stage)
- One listening channel available to the host: MCP tool `read_audio` (native audio modality)
  or `read_media` (external multimodal model). See `skills/video2book/references/install.md`.

## Installation

There is **no packaged OpenCode plugin** in this repository (no `package.json`, no
`.opencode/plugins/*.js`), so install the skill as a **directory**:

```bash
# user level (candidate path — confirm against your own OpenCode config)
ln -s "<this-repo>/skills/video2book" ~/.config/opencode/skills/video2book

# or project level
ln -s "<this-repo>/skills/video2book" "<project>/.opencode/skills/video2book"
```

On Windows prefer a junction (no developer mode required):

```powershell
cmd /c mklink /J "$HOME\.config\opencode\skills\video2book" "<this-repo>\skills\video2book"
```

Prefer symlink/junction over copying: when the repo updates, the installed skill updates with it.

> **Unconfirmed path**: OpenCode's exact skills directory is **not documented publicly** as far as
> this project could verify. `~/.config/opencode/skills/` is only a candidate (OpenCode's own config
> lives at `~/.config/opencode/opencode.jsonc`). Ask OpenCode where it loads skills from, or point its
> skill path config at `<this-repo>/skills/video2book` — then match the directory name to what it expects.

Restart OpenCode and verify by asking it to list its skills, or by asking:
"把 B 站这个网课重构成教材和复习笔记".

## Usage

Use OpenCode's native `skill` tool:

```
use skill tool to list skills
use skill tool to load video2book
```

The skill's commands are relative to the **skill directory** (`skills/video2book/`).
Once installed, that directory is the skill root — run its CLI from there:

```bash
python src/cli.py info
```

## Updating

If you installed via symlink/junction, `git pull` in this repository is enough — nothing to reinstall.
If you copied the directory, re-copy it after pulling.

## Troubleshooting

### Skill not found

1. Use the `skill` tool to list what has been discovered
2. Confirm your OpenCode skills directory actually matches where you put the link
3. Confirm the linked directory contains both `SKILL.md` and the toolchain (`src/`, `scripts/`)

### Tool mapping

The skill speaks in **actions** ("read a file", "write a file", "run a command",
"dispatch a subagent", "listen to an audio slice"). On OpenCode these resolve to:

- "Read a file" → `read`
- "Create a file" / "edit a file" / "delete a file" → `apply_patch`
- "Run a shell/CLI command" → `bash`
- "Search file contents" / "find files by name" → `grep`, `glob`
- "Fetch a URL" → `webfetch`
- "Create a todo" / "mark complete in todo list" → `todowrite`
- "Dispatch a subagent" → `task` tool with `subagent_type: "general"` (or `"explore"`)
- "Listen to the audio slice natively" → MCP tool `read_audio` (requires the `omni-media` MCP);
  if the host has no native audio, use `read_media` from the `omni-media-ext` MCP instead

> Trust your actual tool list over this table when they disagree.

## Getting Help

- Issues: https://github.com/LINJIANG12/video2book/issues
- Full install matrix: `skills/video2book/references/install.md`
