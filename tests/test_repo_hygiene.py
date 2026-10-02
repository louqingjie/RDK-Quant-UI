# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""仓库卫生：那些"踩过一次就不该再踩"的坑。

这里守的不是功能，是**曾经真的把东西弄坏过**的两类问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

#: 参与检查的文本文件类型
TEXT_SUFFIXES = (".py", ".sh", ".cmd", ".bat", ".md", ".toml", ".json", ".cfg")

#: 目录级跳过
SKIP_DIRS = {".git", ".venv", ".build", "dist", "build", "__pycache__", "runs", "shots"}


def _text_files(*suffixes: str):
    for suffix in suffixes:
        for path in REPO.rglob(f"*{suffix}"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


# --------------------------------------------------------------------------
# ① 批处理文件必须是纯 ASCII
# --------------------------------------------------------------------------
def test_cmd_files_are_pure_ascii():
    """`.cmd` / `.bat` 里**一个中文字都不能有**。

    cmd.exe 按 **OEM 码页**（中文 Windows 上是 936/GBK）解码批处理文件，而不是
    UTF-8。中文注释会被解成乱码字节，cmd 把它们当命令执行——而且**会吃掉后面
    代码行的字符**，于是真正的代码也坏了。

    实测现象（scripts/run-windows.cmd）：
        注释里的中文 → 乱码，报"不是内部或外部命令"
        `%RDKQUANT_WSL_DIR%` 被啃成 `QUANT_WSL_DIR` → 报同样错
        `wsl.exe -e bash ...` 被啃掉前面 → 只剩 `-e` 被当成命令

    说明文字放在 `.sh` 或 README 里（那边是 UTF-8 环境，不受影响）。
    """
    offenders = []
    for path in _text_files(".cmd", ".bat"):
        raw = path.read_bytes()
        if not raw.isascii():
            bad = [
                (i + 1, line[:60])
                for i, line in enumerate(raw.decode("utf-8", "replace").splitlines())
                if not line.isascii()
            ]
            offenders.append(f"{path.relative_to(REPO)}：{bad[:3]}")
    assert not offenders, (
        "批处理文件里有非 ASCII 字符，会让 cmd.exe 解析错乱：\n  "
        + "\n  ".join(offenders)
    )


def test_there_is_at_least_one_windows_launcher():
    """别把启动器删了——Windows 用户的入口就靠它。"""
    assert list(SCRIPTS.glob("*.cmd")) or list(SCRIPTS.glob("*.bat"))


# --------------------------------------------------------------------------
# ② 文本文件不能有 BOM
# --------------------------------------------------------------------------
@pytest.mark.parametrize("suffix", TEXT_SUFFIXES)
def test_no_utf8_bom(suffix: str):
    """UTF-8 BOM 会让 Python 直接语法错误，也会让 shell 脚本第一行失效。

    这是**实际造成过损失**的：用 PowerShell 的 `Set-Content -Encoding utf8` 改
    源码，它会加上 BOM，接着 `ast.parse` 就报
    "invalid non-printable character U+FEFF"。
    Python 文件本该是 UTF-8，但不带 BOM。
    """
    offenders = [
        str(path.relative_to(REPO))
        for path in _text_files(suffix)
        if path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    assert not offenders, f"这些文件有 UTF-8 BOM，应当去掉：{offenders}"


# --------------------------------------------------------------------------
# ③ 源码里不能出现 Windows 路径分隔符的硬编码
# --------------------------------------------------------------------------
def test_no_hardcoded_windows_paths_in_core():
    r"""core 要能在容器（Linux）里跑，不能硬编码 `C:\...` 这样的路径。"""
    offenders = []
    for path in (REPO / "src" / "rdkquant" / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # 允许出现在注释/文档里的反斜杠示例，只查真正的字符串字面量
        if 'r"C:\\' in text or '"C:\\\\' in text or "'C:\\\\" in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"core 里出现了硬编码的 Windows 盘符路径：{offenders}"
