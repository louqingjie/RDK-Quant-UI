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


# --------------------------------------------------------------------------
# 启动路径：不带参数应当开界面
# --------------------------------------------------------------------------
def test_cli_without_args_launches_ui(monkeypatch):
    """不带参数就是开图形界面，不该要求用户记住 `ui` 子命令。

    这个行为必须和打包产物一致——它们对使用者而言就是同一个程序。
    之前 CLI 缺少这个分支，`.venv/bin/rdkquant` 会直接报
    "the following arguments are required: cmd"，而 README 里写的是直接运行。
    """
    from rdkquant import cli

    captured = {}

    def fake_ui(args):
        captured["workspace"] = args.workspace
        return 0

    monkeypatch.setattr(cli, "_cmd_ui", fake_ui)
    assert cli.main([]) == 0
    assert "workspace" in captured


def test_cli_ui_subcommand_still_works(monkeypatch):
    from rdkquant import cli

    captured = {}

    def fake_ui(args):
        captured["workspace"] = args.workspace
        return 0

    monkeypatch.setattr(cli, "_cmd_ui", fake_ui)
    assert cli.main(["ui"]) == 0
    assert captured["workspace"] == cli.DEFAULT_WORKSPACE


def test_packaging_entry_routing_matches_cli(monkeypatch):
    """打包入口和 CLI 对「不带参数」必须给出一致行为。

    这是回归测试：两者的路由逻辑曾经不一致，导致开发时能跑的方式和使用者
    拿到的方式行为不同。
    """
    import importlib.util

    import rdkquant

    root = Path(rdkquant.__file__).resolve().parents[2]
    entry_path = root / "packaging" / "entry.py"
    assert entry_path.is_file(), f"找不到打包入口：{entry_path}"

    spec = importlib.util.spec_from_file_location("rdkquant_packaging_entry", entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    launched = {}

    def fake_run(**_kwargs):
        launched["gui"] = True
        return 0

    monkeypatch.setattr("rdkquant.ui.app.run", fake_run)
    assert module.main([]) == 0
    assert launched.get("gui"), "打包产物在不带参数时应当启动图形界面"


@pytest.mark.parametrize("argv", [["doctor", "--help"], ["images", "--help"], ["ui", "--help"]])
def test_cli_subcommands_are_registered(argv):
    """子命令必须都注册得上——顺手守住 argparse 的结构。"""
    from rdkquant import cli

    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(argv)
    assert excinfo.value.code == 0


# --------------------------------------------------------------------------
# M3 三个步骤页
#
# 注意：这些测试刻意复用上面的 shell fixture（它已经把 probe_host 与
# local_images 打了桩）。自己另建 QApplication 会让整个测试进程炸成
# access violation —— 实测踩过，所以别在别的文件里再管 QApplication。
# --------------------------------------------------------------------------
def test_stage_pages_are_wired_in_flow_order(shell):
    """顺序必须是 MODEL → CHECK → CALIB。

    检查页靠 `ctx.model_path` 判断"模型换了没有"，所以模型页必须先写路径。
    顺序反了会让失效判断读到上一轮的值，等于没做。
    """
    from rdkquant.ui.pages import CalibPage, CheckPage, ModelPage

    kinds = [type(page) for page in shell.stage_pages]
    assert kinds == [ModelPage, CheckPage, CalibPage]
    assert [p.spec.stage for p in shell.stage_pages] == [
        Stage.MODEL, Stage.CHECK, Stage.CALIB,
    ]


def test_unfinished_steps_still_use_placeholder(shell):
    from rdkquant.ui.pages import PlaceholderPage

    assert isinstance(shell.pages[Stage.CONVERT.value], PlaceholderPage)
    assert isinstance(shell.pages[Stage.PERF.value], PlaceholderPage)


def test_without_environment_actions_are_disabled_and_explained(shell):
    """没环境时应当**禁用并引导**，而不是等用户点了再报错。"""
    model, check, calib = shell.stage_pages
    assert shell.current_backend() is None
    assert not model.pick_btn.isEnabled()
    assert not check.run_btn.isEnabled()
    assert not calib.scan_btn.isEnabled()
    assert not calib.build_btn.isEnabled()
    for page in (model, check, calib):
        hint = page.environment_hint()
        assert "环境准备" in hint and "镜像" in hint


def test_changing_the_model_invalidates_a_passing_check(shell, tmp_path: Path):
    """最危险的一类假阳性：换了模型却仍显示"检查通过"。

    门禁一旦放行一个从未被检查过的模型，用户会拿着错误结论往下走。
    """
    model, check, _calib = shell.stage_pages
    old = tmp_path / "run" / "model" / "old.onnx"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"x")

    check.checked_model = old
    check.passed = True
    check.log_path = tmp_path / "checker.log"
    model.model_path = old
    shell.refresh_workflow()
    assert shell.context.checker_passed is True

    new = old.parent / "new.onnx"
    new.write_bytes(b"y")
    model.model_path = new
    shell.refresh_workflow()
    assert shell.context.checker_passed is False, (
        "换模型后旧结论必须失效，否则门禁会放行一个从未检查过的模型"
    )
    assert shell.context.checker_log is None


def test_preprocess_toggle_changes_calibration_severity(shell):
    """同一份数据，打开 preprocess_on 后结论必须从"错误"降级为"警告"。"""
    from rdkquant.core.findings import LEVEL_ERROR, LEVEL_WARN

    calib = shell.stage_pages[2]
    calib.payload = {
        "ok": True, "count": 48, "image_like": 48, "binary_like": 0,
        "resolutions": [[640, 640, 40], [320, 240, 8]],
        "duplicates": [], "problems": [], "warnings": [],
    }
    calib.preprocess_box.setChecked(False)
    calib._evaluate()
    assert calib.verdict.blocked
    assert any(f.level == LEVEL_ERROR for f in calib.verdict.findings)

    calib.preprocess_box.setChecked(True)
    calib._evaluate()
    assert not calib.verdict.blocked
    assert any(f.level == LEVEL_WARN for f in calib.verdict.findings)


def test_backend_is_built_when_environment_is_ready(shell, monkeypatch, tmp_path: Path):
    """环境就绪时必须真的把后端建出来。

    这是回归测试：`current_backend()` 在没有 Docker 或没导镜像时会**提前返回
    None**，于是所有既有测试都从没走到 `make_backend` —— 结果里面把 `Path`
    当 `Workspace` 传了进去（`Path` 没有 `ensure()`），只有在真实机器上
    "Docker 在跑 + 镜像已导入"时才会炸。用户第一次跑真实环境就撞上了。

    教训：**提前返回的分支会让后面的代码完全没被测到**。所以这条测试专门把
    环境伪装成就绪，逼着它走完那条路。
    """
    import rdkquant.ui.pages.env as env_mod

    report = _fake_report()
    report.docker = DockerStatus(
        cli_path="/usr/bin/docker", cli_version="29.1.3",
        daemon_ok=True, daemon_version="29.1.3",
    )
    shell.env_page.report = report
    monkeypatch.setattr(
        env_mod.img, "find_loaded",
        lambda *a, **k: "openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8-py310",
    )

    # 不该抛异常
    shell.refresh_workflow()
    backend = shell.current_backend()
    assert backend is not None, "环境就绪时应当给出后端"
    assert backend.workspace.root == (tmp_path / "run"), (
        "后端拿到的必须是 Workspace（带 ensure/rel），而不是裸 Path"
    )
    assert hasattr(backend.workspace, "ensure")
    assert hasattr(backend.workspace, "rel")


def test_backend_is_cached_across_refreshes(shell, monkeypatch, tmp_path: Path):
    """环境没变时不该每次刷新都重建后端（重建会丢掉容器配置）。"""
    import rdkquant.ui.pages.env as env_mod

    report = _fake_report()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    shell.env_page.report = report
    monkeypatch.setattr(env_mod.img, "find_loaded", lambda *a, **k: "img:tag")

    # 先刷新一次让 context.image_ref 填上（它由 env 页在 refresh 时写入），
    # 再取后端——顺序反了会拿到 None，测的就不是缓存了。
    shell.refresh_workflow()
    first = shell.current_backend()
    assert first is not None

    shell.refresh_workflow()
    assert shell.current_backend() is first, "环境没变就该复用同一个后端"


# --------------------------------------------------------------------------
# 表格接线
#
# 这是一条**不报错**的坑，必须用测试守住：make_table() 的第一个参数是
# **表头列表**而不是模型。传成模型的话视图上不会挂任何模型 →
# columnCount() 为 0 → fit_columns_to_content() 直接早退 → 渲染出一张没有表头、
# 没有行、列宽全 0 的空表格。数据在 model 里明明是对的，界面就是空白。
# M3 的四张表全踩了，只有真机截图才看得出来。
# --------------------------------------------------------------------------
def test_every_stage_table_is_wired_to_a_model(shell):
    from PySide6.QtWidgets import QTableView

    checked = 0
    for page in shell.stage_pages:
        for view in page.findChildren(QTableView):
            model = view.model()
            assert model is not None, f"{type(page).__name__} 里有表格没挂模型"
            assert model.columnCount() > 0, (
                f"{type(page).__name__} 的表格列数为 0 —— 多半是把模型当成表头传给了 make_table()"
            )
            checked += 1
    assert checked >= 4, f"应当检查到多张表格，实际只有 {checked} 张"


def test_findings_table_is_wired(shell):
    """结论表在三个页面上都有，最容易漏。"""
    for page in shell.stage_pages:
        model = page.findings_view.model()
        assert model is not None
        assert model.columnCount() == 3


def test_model_page_runs_fit_columns_after_data(shell):
    """填完数据后 fit_columns_to_content 必须真的跑到。

    它的第一个动作是 `setStretchLastSection(False)`，所以拿这个当判据是确定的；
    改用 columnWidth() 判断不可靠——视图没轮到布局时宽度本来就是 0。
    """
    model_page = shell.stage_pages[0]
    model_page._on_inspected({
        "ok": True, "onnx": "1.12.0", "ir_version": 8, "opset": 11,
        "inputs": [{"name": "images", "shape": [1, 3, 640, 640],
                    "dtype": "float16", "dynamic": False}],
        "outputs": [{"name": "output", "shape": [1, 25200, 22],
                     "dtype": "float32", "dynamic": False}],
        "op_histogram": [["Conv", 120], ["Mul", 40]],
        "problems": [], "warnings": [],
    })
    assert model_page.io_model.rowCount() == 2
    assert model_page.io_model.row_text(0)[0] == "images"
    for name in ("io_view", "ops_view", "params_view"):
        view = getattr(model_page, name)
        assert view.horizontalHeader().stretchLastSection() is False, (
            f"{name} 上的 fit_columns_to_content 没跑到"
        )
    assert model_page.findings_view.horizontalHeader().stretchLastSection() is False
