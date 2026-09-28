"""从 queue_tracker 取写作派发载荷，逐条打印 dispatch_prompt（原样透传给子智能体）。"""
import subprocess
import sys
import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "video2book"


def main() -> int:
    task, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 6
    out = subprocess.run(
        [sys.executable, "scripts/queue_tracker.py", "--task", task, "--next-module", str(n), "--json"],
        cwd=SKILL, capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        print("queue_tracker 失败:", out.stderr[:800], file=sys.stderr)
        return 1
    d = json.loads(out.stdout)
    items = d.get("next") or []
    print(f"### 本批 {len(items)} 块（剩余待写 {d.get('pending')}）", file=sys.stderr)
    for it in items:
        print(f"\n===== BLOCK {it['block_id']} | {it['title']} =====")
        print(it["dispatch_prompt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
