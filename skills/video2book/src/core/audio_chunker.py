"""Audio helpers: probing duration and formatting time strings.

取音早已收敛到「块」这一层（见 `audio_merger`）：本模块只保留两个被块级链路复用的纯工具——
`get_audio_duration`（ffprobe 优先、ffmpeg -i 兜底）与 `format_seconds`（HH:MM:SS）。
为单集音频切片的那条老链路（`chunk_audio`）已随「逐集听音」整体移除，不要回加。
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Union

from .local_media import PROBE_TIMEOUT_SEC
from .proc import run_quiet


class AudioChunker:
    @staticmethod
    def get_audio_duration(audio_filepath: str) -> float:
        """Get audio file duration in seconds using ffprobe or ffmpeg."""
        ffprobe_bin = shutil.which("ffprobe")
        if ffprobe_bin:
            cmd = [
                ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_filepath),
            ]
            res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=PROBE_TIMEOUT_SEC)
            if res.returncode == 0 and res.stdout.strip():
                try:
                    return float(res.stdout.strip())
                except ValueError:
                    pass

        # Fallback to ffmpeg -i parsing
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            cmd = [ffmpeg_bin, "-i", str(audio_filepath)]
            res = run_quiet(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=PROBE_TIMEOUT_SEC)
            output = res.stderr
            # Parse Duration: 00:40:09.12
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", output)
            if m:
                hours = float(m.group(1))
                minutes = float(m.group(2))
                seconds = float(m.group(3))
                return hours * 3600 + minutes * 60 + seconds

        return 0.0

    @staticmethod
    def format_seconds(seconds: float) -> str:
        """Format seconds into HH:MM:SS string."""
        s = int(round(seconds))
        hours = s // 3600
        minutes = (s % 3600) // 60
        secs = s % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
