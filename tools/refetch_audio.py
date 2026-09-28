"""为「字幕错配」的分集单独取音频（工具不会为它们下载音频，因为字幕表面完整）。"""
import os
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "video2book"
sys.path.insert(0, str(SKILL))

from src.core.ingestion.coordinator import get_coordinator  # noqa: E402
from src.core.credentials import resolve_sessdata  # noqa: E402

URL = os.environ.get("BVB_REFETCH_URL", "https://www.bilibili.com/video/BV12t4115735")
PAGES = [int(x) for x in sys.argv[1:]] or [8, 9, 14]
OUT = Path(sys.argv[0]).resolve().parent.parent / "skills" / "video2book" / "output" / "_refetch"

coord = get_coordinator()
info = coord.resolve_target_info(URL)
sess = resolve_sessdata()
parts = [p for p in (info.get("parts") or []) if isinstance(p, dict)]
OUT.mkdir(parents=True, exist_ok=True)

for pg in PAGES:
    ep = next((p for p in parts if int(p.get("page") or 0) == pg), None)
    if not ep:
        print(f"P{pg:02d}: 未找到")
        continue
    目标 = OUT / f"P{pg:02d}.m4a"
    print(f"P{pg:02d} {ep.get('title')} -> {目标.name}", flush=True)
    try:
        coord.fetch_episode_audio(
            info, ep, 目标, force=True, sessdata=sess, quality="low",
        )
        print(f"  OK {目标.stat().st_size:,} 字节")
    except Exception as e:
        print(f"  FAIL {type(e).__name__}: {e}")
