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


# --------------------------------------------------------------------------
# ④ UI 里不能有「已经被证伪」的说法
# --------------------------------------------------------------------------
#: 每条都要写清**为什么过时**，否则以后没人敢删。
#:
#: ⚠️ 这**不是主动发现机制** —— 它只能把已经修好的锁住，防止漂回去。
#: 新发现一条「文案和代码相反」，就在这儿加一行。
STALE_UI_CLAIMS = (
    ("缩略图由容器生成", "缩略图早已改成宿主机按需解码（ui/calibimages.py）"),
    ("容器里没有可用的图像库", "扫描搬到宿主机之后，这句会对每个用户显示"),
)


def _string_literals(path: Path):
    """文件里的**字符串字面量**（含 docstring），`(行号, 内容)`。

    **只取字符串，不取注释。** 这不是洁癖：记录一个 bug 的注释里会**引用**
    那句话（`calib.py` 里就有一处写着「容器里没有可用的图像库」来解释它为什么
    错），按整文件 grep 的话，**注释反而会把测试弄红**。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _ui_python_files():
    for path in (REPO / "src" / "rdkquant" / "ui").rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def test_ui_text_has_no_stale_claims():
    """UI 里不能再出现**已经被证伪**的说法。

    `docs/AGENTS.md` 把「文案和代码相反」列为最危险的一类：缺文档只是没人看，
    **写反的文案会把人引向错误的修改**。本项目真实发生过两次，都是
    **截图核对**发现的，不是测试：

    * 校准页卡片副标题写着「缩略图由容器生成」—— 早已搬到宿主机
    * `_evaluate()` 掉进兜底分支，对每个用户显示「容器里没有可用的图像库」

    所以这条测试的定位要说清：它是**回归门禁**，不是发现新问题的机制。
    """
    offenders = []
    for path in _ui_python_files():
        for line, text in _string_literals(path):
            for phrase, why in STALE_UI_CLAIMS:
                if phrase in text:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{line} 有 {phrase!r}（{why}）"
                    )
    assert not offenders, (
        "UI 里还有过时的说法：\n  " + "\n  ".join(offenders)
    )


def test_stale_claim_scanner_actually_sees_strings():
    """**自检：确认上面那条门禁不是空的。**

    取不到字符串的话，`test_ui_text_has_no_stale_claims` 永远绿 ——
    那就是"看起来有门禁其实没有"，比没有更糟。
    """
    literals = list(_string_literals(REPO / "src" / "rdkquant" / "ui"
                                     / "pages" / "calib.py"))
    assert len(literals) > 50, f"AST 只取到 {len(literals)} 个字符串，取法有问题"

    texts = [value for _line, value in literals]
    assert not any("容器里没有可用的图像库" in t for t in texts), (
        "**注释里的说明不该被算进去** —— 那句话在 calib.py 里是用来解释这个 bug 的，"
        "按整文件 grep 会把记录它的注释弄红"
    )
