"""Local Media Processor & Universal Audio Extractor.

Supports:
1. Scanning single local video/audio files or entire course directories.
2. Natural sorting of video parts (e.g. 01, 02, 第1讲, 第2讲).
3. Universal audio extraction via FFmpeg (all video formats -> 64kbps 16kHz mono AAC .m4a).
4. Providing standard metadata schema compatible with BilibiliParser for downstream pipelines.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from .proc import run_quiet

SUPPORTED_VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".ts", ".m4v", ".rmvb"
}
SUPPORTED_AUDIO_EXTS = {
    ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma"
}
SUPPORTED_MEDIA_EXTS = SUPPORTED_VIDEO_EXTS | SUPPORTED_AUDIO_EXTS

# 子进程硬超时（秒）：探测类操作用短超时，转码/切片类用长超时上限。
# 上限 600s 依据：2 小时课程视频的纯音频提取在慢速磁盘上的保守估时；若超时请先切片再处理。
PROBE_TIMEOUT_SEC = 15
TRANSCODE_TIMEOUT_SEC = 600


def natural_sort_key(s: str) -> list:
    """Sort strings with embedded numbers naturally (e.g. 'P2' before 'P10')."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


class LocalMediaParser:
    @staticmethod
    def is_local_media(path_str: Union[str, Path]) -> bool:
        """Check if the given string points to an existing local video, audio, or media directory."""
        try:
            p = Path(path_str).resolve()
            if not p.exists():
                from src.core import paths as _paths
                cand = _paths.home_root() / path_str
                if cand.exists():
                    p = cand
                else:
                    cand_prod = _paths.products_root() / path_str
                    if cand_prod.exists():
                        p = cand_prod
                    else:
                        return False
            if p.is_file():
                return p.suffix.lower() in SUPPORTED_MEDIA_EXTS
            if p.is_dir():
                return any(
                    f.is_file() and f.suffix.lower() in SUPPORTED_MEDIA_EXTS
                    for f in p.rglob("*")
                )
        except Exception:
            return False
        return False

    @staticmethod
    def get_duration(filepath: Union[str, Path]) -> float:
        """Probe media file duration in seconds using ffprobe or ffmpeg."""
        target = Path(filepath).resolve()
        if not target.exists():
            return 0.0

        ffprobe_bin = shutil.which("ffprobe")
        if ffprobe_bin:
            cmd = [
                ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(target),
            ]
            try:
                res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=PROBE_TIMEOUT_SEC)
                if res.returncode == 0 and res.stdout.strip():
                    return float(res.stdout.strip())
            except Exception:
                pass

        # Fallback to ffmpeg -i parsing
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            cmd = [ffmpeg_bin, "-i", str(target)]
            try:
                res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=PROBE_TIMEOUT_SEC)
                output = res.stderr
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", output)
                if m:
                    hours = float(m.group(1))
                    minutes = float(m.group(2))
                    seconds = float(m.group(3))
                    return hours * 3600 + minutes * 60 + seconds
            except Exception:
                pass

        return 0.0

    @classmethod
    def extract_audio(
        cls,
        video_path: Union[str, Path],
        output_audio_path: Union[str, Path],
    ) -> Path:
        """Universal audio extractor: extracts 64kbps 16kHz mono AAC audio from any video format.
        
        Guarantees compatibility with all video containers and multi-channel audio tracks.
        """
        src = Path(video_path).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        target = Path(output_audio_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        # If already a .m4a and target is identical, return directly
        if src == target:
            return target

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError(
                "未检测到 FFmpeg 命令行工具。请安装 FFmpeg 并添加至系统 PATH (例如 Windows: winget install Gyan.FFmpeg)"
            )

        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", str(src),
            "-vn",
            "-map", "0:a:0?",
            "-c:a", "aac",
            "-b:a", "64k",
            "-ar", "16000",
            "-ac", "1",
            str(target),
        ]
        try:
            res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=TRANSCODE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(
                f"FFmpeg 音频提取超时（>{TRANSCODE_TIMEOUT_SEC}s）：{src.name}。建议先用 ffmpeg 切片后再处理。"
            ) from err
        if res.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            err_msg = res.stderr[-400:] if res.stderr else "未知错误"
            raise RuntimeError(f"从视频提取音频失败（可能无有效音频轨）: {err_msg}")

        return target

    @classmethod
    def parse(cls, path_str: Union[str, Path]) -> Dict[str, Any]:
        """Parse local video file or course directory into standard metadata schema."""
        target = Path(path_str).resolve()
        if not target.exists():
            from src.core import paths as _paths
            cand = _paths.home_root() / path_str
            if cand.exists():
                target = cand
            else:
                cand_prod = _paths.products_root() / path_str
                if cand_prod.exists():
                    target = cand_prod
        if not target.exists():
            raise FileNotFoundError(f"Local media path does not exist: {path_str}")

        media_files: List[Path] = []
        if target.is_file():
            if target.suffix.lower() not in SUPPORTED_MEDIA_EXTS:
                raise ValueError(f"不支持的媒体文件类型: {target.suffix}")
            media_files = [target]
            task_title = target.stem
        else:
            # Directory: scan all supported media files recursively
            media_files = [
                f for f in target.rglob("*")
                if f.is_file() and f.suffix.lower() in SUPPORTED_MEDIA_EXTS
            ]
            if not media_files:
                raise ValueError(f"指定目录下未找到任何受支持的视频或音频文件: {target}")
            # Natural sort by relative path
            media_files.sort(key=lambda f: natural_sort_key(str(f.relative_to(target))))
            task_title = target.name

        safe_title = re.sub(r'[\\/*?:"<>|\n\r\t]+', '_', task_title).strip(" ._-")[:80]
        if not safe_title:
            safe_title = "local_course"

        parts = []
        total_duration = 0.0
        for idx, f in enumerate(media_files, 1):
            dur = cls.get_duration(f)
            total_duration += dur
            rel = f.relative_to(target) if target.is_dir() else Path(f.name)
            part_title = f"{rel.parent.as_posix()} - {f.stem}" if rel.parent != Path(".") else f.stem
            parts.append({
                "page": idx,
                "title": part_title,
                "cid": f"local_{idx:03d}",
                "duration": int(dur),
                "filepath": str(f),
                "url": f.as_uri(),
            })

        has_multi = len(parts) > 1
        video_type = "multi_page" if has_multi else "single"
        type_desc = f"本地多集课程 (共 {len(parts)} P)" if has_multi else "本地单视频"

        return {
            "bvid": f"local_{safe_title}",
            "title": task_title,
            "desc": f"本地视频任务: {target.name}",
            "duration": int(total_duration),
            "owner": {"name": "本地媒体", "mid": 0},
            "video_type": video_type,
            "type_desc": type_desc,
            "cid": parts[0]["cid"] if parts else "local_001",
            "has_multi_pages": has_multi,
            "has_ugc_season": False,
            "season_episodes": [],
            "parts": parts,
            "is_local": True,
            "source_path": str(target),
        }
