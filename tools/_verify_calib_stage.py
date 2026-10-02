"""用真实数据集验证：工作区外的目录现在能不能跑通校准扫描。"""
import os
import tempfile
import time
from pathlib import Path

from rdkquant.core.backends import Workspace, make_backend

SOURCE = Path(r"C:\Users\ccron\Desktop\RM2025-Armor-Public-Dataset")
IMAGE = "openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8-py310"

ws = Workspace(Path("runs/current")).ensure()
backend = make_backend(workspace=ws, image=IMAGE, march="bayes-e")
print("  后端:", type(backend).__name__)

# 复刻 CalibPage._stage 的逻辑（不构造界面）
import shutil  # noqa: E402

from rdkquant.ui.pages.calib import IMAGE_SUFFIXES  # noqa: E402

target = ws.root / "cal_images"
target.mkdir(parents=True, exist_ok=True)
pending = [p for p in sorted(SOURCE.iterdir())
           if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
print(f"  源目录图片: {len(pending)} 张")

start = time.perf_counter()
copied = 0
for path in pending:
    dest = target / path.name
    if not (dest.exists() and dest.stat().st_size == path.stat().st_size):
        shutil.copy2(path, dest)
    copied += 1
elapsed = time.perf_counter() - start
total_mb = sum(p.stat().st_size for p in pending) / 1024 / 1024
print(f"  收进工作区: {copied} 张 / {total_mb:.1f} MB，用时 {elapsed:.1f} 秒")

print()
print("  === 扫描（容器里跑探针）===")
lines: list[str] = []
# 全角乘号，复刻用户日志里那一幕
payload = backend.scan_calib(
    target, shape="1×3×640×640", dtype="float32", on_line=lines.append,
)
print("  ok           :", payload.get("ok"))
print("  imaging      :", payload.get("imaging_backend"))
print("  count        :", payload.get("count"))
print("  image_like   :", payload.get("image_like"))
print("  resolutions  :", (payload.get("resolutions") or [])[:3])
print("  problems     :", (payload.get("problems") or [])[:3])
print("  warnings     :", (payload.get("warnings") or [])[:3])
print("  exposure     :", payload.get("exposure"))
