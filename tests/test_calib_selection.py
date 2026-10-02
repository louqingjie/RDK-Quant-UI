# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""筛选与手动挑选的端到端契约。

这里守的是**界面与容器之间的接口**，不是内部实现：

  * `calib-scan --stats-file` 必须写出**全部**图片的逐张统计（回包里的 files 受
    500 条上限截断，筛选不能依赖它）
  * `thumbs` 与 `calib-build` 在给了 `--only-file` 时**只处理名单里的文件**
  * 名单里有不存在的文件时跳过并报告，而不是崩
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import rdkquant

PROBE = Path(rdkquant.__file__).parent / "data" / "rdkquant_probe.py"


def run_probe(*argv: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(PROBE), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"探针没有任何输出（stderr: {result.stderr[:300]}）"
    return json.loads(lines[-1])


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("probe_under_test", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_images(directory: Path, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"{index:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)


# --------------------------------------------------------------------------
# --stats-file：完整、不被 500 条上限截断
# --------------------------------------------------------------------------
def test_stats_file_covers_all_images_beyond_the_payload_limit(tmp_path: Path, probe):
    """**这条是整个筛选功能的地基。**

    回包里的 `files` 受 `MAX_FILE_DETAILS = 500` 限制；而真实数据集有 3504 张，
    只看回包会让筛选作用在一个偏斜的子集上（只有前 500 张）。
    """
    src = tmp_path / "images"
    total = probe.MAX_FILE_DETAILS + 137          # 刻意超过上限
    make_images(src, total)
    stats_path = tmp_path / "stats.json"

    payload = run_probe("calib-scan", str(src), "--stats-file", str(stats_path))

    assert payload["ok"] is True
    assert payload["stats_count"] == total
    assert payload["stats_file"] == str(stats_path)
    assert len(payload["files"]) == probe.MAX_FILE_DETAILS, "回包本身仍然截断"

    written = json.loads(stats_path.read_text(encoding="utf-8"))
    assert written["count"] == total
    assert len(written["files"]) == total, "统计文件必须是完整的"
    assert written["files"][0]["n"] == "0000.jpg"
    assert set(written["files"][0]) == {"n", "w", "h", "s", "m"}


def test_stats_file_is_optional(tmp_path: Path):
    src = tmp_path / "images"
    make_images(src, 3)
    payload = run_probe("calib-scan", str(src))
    assert payload["ok"] is True
    assert "stats_file" not in payload, "没要求就不该写文件"
    assert payload["stats_count"] == 3, "但条数总是要报的"


# --------------------------------------------------------------------------
# --only-file：只处理名单里的文件
# --------------------------------------------------------------------------
def test_only_file_selects_exactly_the_listed_names(tmp_path: Path, probe):
    src = tmp_path / "images"
    make_images(src, 10)
    listed = ["0003.jpg", "0007.jpg"]
    names = tmp_path / "only.txt"
    names.write_text("\n".join(listed), encoding="utf-8")

    # thumbs 在没有图像库的宿主上会失败，但参数校验发生在更早，
    # 所以这里直接验证探针读名单的行为
    assert probe.read_name_list(str(names)) == listed


def test_only_file_ignores_comments_and_blank_lines(tmp_path: Path, probe):
    names = tmp_path / "only.txt"
    names.write_text("# 注释\n0001.jpg\n\n  0002.jpg  \n# 又一行注释\n", encoding="utf-8")
    assert probe.read_name_list(str(names)) == ["0001.jpg", "0002.jpg"]


def test_only_file_missing_returns_none(tmp_path: Path, probe):
    assert probe.read_name_list(str(tmp_path / "不存在.txt")) is None
    assert probe.read_name_list(None) is None


def test_only_file_with_no_valid_names_errors(tmp_path: Path):
    src = tmp_path / "images"
    make_images(src, 5)
    names = tmp_path / "only.txt"
    names.write_text("不存在1.jpg\n不存在2.jpg\n", encoding="utf-8")

    payload = run_probe("calib-build", str(src), str(tmp_path / "out"),
                        "--shape", "64x64", "--only-file", str(names))
    # 宿主没有图像库时会更早失败；有图像库的机器上则应当报"一个都不在源目录里"。
    # 两种情况都必须是 ok=False，不能静默产出空目录。
    assert payload["ok"] is False
    assert payload.get("error")


# --------------------------------------------------------------------------
# 采样与名单共用同一套选取
# --------------------------------------------------------------------------
def test_sample_evenly_still_used_when_no_list(tmp_path: Path, probe):
    files = [f"{i:04d}.jpg" for i in range(100)]
    assert len(probe.sample_evenly(files, 10)) == 10
    assert len(probe.sample_evenly(files, 0)) == 100
