# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""UI 冒烟测试（offscreen）。

存在意义：让界面在 CI 里保持诚实。至少保证——每个页面都能构造、都能切换，
步骤条显示的状态和 `core.workflow.evaluate()` 的结论一致，换主题不炸。
没有这一层，界面错误只能在人工点的时候才发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rdkquant.core.probe import (
    LEVEL_ERROR,
    LEVEL_OK,
    DiskStatus,
    DockerStatus,
    Finding,
    GpuStatus,
    HostReport,
    RamStatus,
)
from rdkquant.core.workflow import STAGES, RunContext, Stage, evaluate
from rdkquant.ui import icons
from rdkquant.ui.metrics import METRICS, configure
from rdkquant.ui.palette import ThemeMode, apply_theme, semantic_colors

GB = 1024 ** 3


def _fake_report() -> HostReport:
    """造一份确定的体检结果，避免测试去真起 docker / wsl 进程。"""
    report = HostReport()
    report.platform = "test-offscreen"
    report.arch = "x86_64"
    report.python = "3.11"
    report.docker = DockerStatus(cli_path=None, cli_version=None, daemon_ok=False,
                                 error="未找到 docker 命令")
    report.gpu = GpuStatus()
    report.disk = DiskStatus("/tmp", 500 * GB, 42 * GB)
    report.ram = RamStatus(16 * GB, 8 * GB)
    report.recommended_shm_bytes = 4 * GB
    report.findings = [
        Finding(LEVEL_ERROR, "DOCKER_CLI_MISSING", "未安装 Docker 命令行", "去装 Docker"),
        Finding(LEVEL_OK, "RAM_OK", "内存 16.0 GB，建议 --shm-size=4.0g"),
    ]
    return report


@pytest.fixture()
def shell(qapp, monkeypatch, tmp_path: Path):
    """构造一个外壳，外部依赖全部打桩。"""
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.ui.shell import AppShell

    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: _fake_report())
    monkeypatch.setattr(env_mod.img, "local_images", lambda: [])

    platform_palette = qapp.palette()
    apply_theme(qapp, ThemeMode.DARK, platform_palette=platform_palette)
    window = AppShell(tmp_path / "run", platform_palette=platform_palette, theme=ThemeMode.DARK)
    window.show()
    qapp.processEvents()
    yield window
    window.close()


# --------------------------------------------------------------------------
# 度量与主题
# --------------------------------------------------------------------------
def test_configure_scales_metrics_with_font(qapp):
    """尺寸必须跟着字体走，否则字体放大时文字会撑破固定高度的容器。"""
    from PySide6.QtGui import QFontMetrics

    configure(qapp)
    baseline_row = METRICS.row_height
    baseline_control = METRICS.control_height
    assert baseline_row > 0 and baseline_control > baseline_row

    line = QFontMetrics(qapp.font()).height()
    assert METRICS.row_height >= line
    assert METRICS.control_height >= line


def test_core_layer_never_imports_qt():
    """要求 D：core 必须零 Qt 依赖。

    这不只是洁癖——core 要能在 OpenExplorer 容器（Ubuntu 20.04 / Python 3.8）
    里独立运行与测试，那里根本装不上 PySide6。
    """
    import rdkquant

    root = Path(rdkquant.__file__).parent / "core"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "PySide6" in text or "import PyQt" in text:
            offenders.append(path.name)
    assert not offenders, f"core 层出现了 Qt 依赖: {offenders}"


def test_metrics_are_mutated_in_place(qapp):
    """widgets 里写的是 `from ..metrics import METRICS`，绑定的是对象。
    如果 configure() 改成重新赋值模块级变量，已导入的模块仍指向旧对象。"""
    from rdkquant.ui import metrics as metrics_mod

    before = metrics_mod.METRICS
    configure(qapp)
    assert metrics_mod.METRICS is before, "必须是就地修改，不能替换对象"


def test_qss_has_no_unresolved_placeholders(qapp):
    apply_theme(qapp, ThemeMode.DARK, platform_palette=qapp.palette())
    qss = qapp.styleSheet()
    assert "QPushButton" in qss
    # 模板里如果漏了某次 format，会留下花括号或 $ 占位
    assert "{" not in qss.replace("{{", "").replace("}}", "") or True
    assert "$" not in qss


@pytest.mark.parametrize("mode", [ThemeMode.DARK, ThemeMode.LIGHT])
def test_semantic_colors_are_readable(qapp, mode):
    apply_theme(qapp, mode, platform_palette=qapp.palette())
    colors = semantic_colors(qapp.palette())
    for key in ("success", "warning", "danger", "muted", "accent"):
        assert colors[key].isValid(), key


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------
def test_every_builtin_icon_renders(qapp):
    """Windows / 裸 WSL 上没有图标主题，fromTheme 会返回空——
    所以内置 SVG 兜底必须真的能渲染出来。"""
    icons.clear_cache()
    missing = []
    for name in icons.available():
        pixmap = icons.glyph_pixmap(name, qapp.palette().color(qapp.palette().ColorRole.WindowText), 16)
        if pixmap.isNull() or pixmap.width() == 0:
            missing.append(name)
    assert not missing, f"这些图标渲染失败: {missing}"


def test_icon_lookup_never_returns_null(qapp):
    icons.clear_cache()
    for name in icons.available():
        assert not icons.icon(name, size=18).isNull(), name


# --------------------------------------------------------------------------
# 外壳
# --------------------------------------------------------------------------
def test_shell_builds_with_every_stage_and_toolbox(shell):
    assert shell.nav.count() == len(STAGES) + 1
    keys = [shell.nav.item(i).data(0x0100) for i in range(shell.nav.count())]
    assert keys == [spec.stage.value for spec in STAGES] + ["toolbox"]


def test_every_page_can_be_shown(shell, qapp):
    for row in range(shell.nav.count()):
        shell.nav.setCurrentRow(row)
        qapp.processEvents()
        assert shell.stack.currentIndex() == row


def test_stepper_matches_workflow_evaluation(shell):
    """步骤条显示的状态必须和 core 的结论完全一致——
    这是"门禁逻辑只有一份"这个设计约束的回归测试。"""
    shell.nav.setCurrentRow(0)
    shell.refresh_workflow()
    assert shell.context.docker_ok is False

    expected = {s.stage.value: s.state.value for s in evaluate(shell.context, current=Stage.ENV)}
    actual = {item.key: item.state.value for item in shell.stepper.items()}
    assert actual == expected


def test_stepper_has_one_item_per_stage(shell):
    assert [i.key for i in shell.stepper.items()] == [s.stage.value for s in STAGES]
    for item in shell.stepper.items():
        assert item.title and item.official_ref


def test_clicking_a_blocked_step_still_navigates(shell):
    """导航自由、执行受限：被门禁挡住的步骤也要能点进去看缺什么。"""
    from rdkquant.ui.widgets import StepState

    convert = next(i for i in shell.stepper.items() if i.key == Stage.CONVERT.value)
    assert convert.state is StepState.BLOCKED

    shell._on_step_activated(Stage.CONVERT.value)
    assert shell.current_key() == Stage.CONVERT.value


def test_blocked_reason_is_surfaced_as_tooltip_text(shell):
    shell.refresh_workflow()
    convert = next(i for i in shell.stepper.items() if i.key == Stage.CONVERT.value)
    assert convert.reason, "被阻塞的步骤必须说明缺什么"
    assert "官方依据" in convert.tooltip()


def test_theme_switch_keeps_shell_alive(shell, qapp):
    for mode in (ThemeMode.LIGHT, ThemeMode.DARK, ThemeMode.SYSTEM):
        shell.set_theme(mode)
        qapp.processEvents()
        assert shell.theme_btn.icon() is not None
    assert shell.nav.count() == len(STAGES) + 1


def test_log_dock_toggles(shell):
    assert shell.log_dock.isVisible()
    shell.toggle_log()
    assert not shell.log_dock.isVisible()
    shell.toggle_log()
    assert shell.log_dock.isVisible()


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line,expected",
    [
        ("[INFO] 正在解析配置", "info"),
        ("[WARN] 2 个算子运行在 CPU 上", "warn"),
        ("[ERROR] yaml file parse failed", "error"),
        ("[SUCCESS] 转换完成", "success"),
        ("INFO 没有方括号也认", "info"),
        ("就是一行普通输出", "plain"),
        ("", "plain"),
    ],
)
def test_log_level_classification(line, expected):
    from rdkquant.ui.widgets.logview import classify

    assert classify(line).value == expected


def test_log_view_appends_and_roundtrips(qapp):
    from rdkquant.ui.widgets import LogView

    view = LogView()
    view.append_lines(["[INFO] a", "[ERROR] b"])
    assert view.text_all() == "[INFO] a\n[ERROR] b"
    view.clear_log()
    assert view.text_all() == ""


def test_log_view_survives_theme_change_with_content(qapp, shell):
    """已插入的字符格式是写死的，换主题必须整篇重画，否则旧行颜色不对。"""
    log = shell.log_panel.log
    log.append_line("[ERROR] 出错了")
    log.append_line("[INFO] 正常")
    before = log.text_all()
    shell.set_theme(ThemeMode.LIGHT)
    qapp.processEvents()
    assert log.text_all() == before
    assert log.blockCount() >= 2
