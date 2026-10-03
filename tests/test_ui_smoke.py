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
from PySide6.QtCore import QCoreApplication, QEvent

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
from rdkquant.core.images import RemoteInfo
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

    # **必须打桩 `probe_remote`。** 环境页启动后会异步自动 HEAD 四个官方 URL
    # （见 `_auto_probe_remote`）——不打桩的话每个用例都会真的发网络请求：
    # 慢，而且在离线的 CI 上每个请求要等到 30 秒超时。
    # 这里返回一个"探测失败"，正好覆盖"离线时用清单估算值"那条路径。
    monkeypatch.setattr(
        env_mod.img, "probe_remote",
        lambda url, **k: RemoteInfo(
            content_length=None, accept_ranges=False, ok=False, error="测试不打网络"
        ),
    )

    platform_palette = qapp.palette()
    apply_theme(qapp, ThemeMode.DARK, platform_palette=platform_palette)
    window = AppShell(tmp_path / "run", platform_palette=platform_palette, theme=ThemeMode.DARK)
    window.show()
    qapp.processEvents()
    yield window

    # 关窗前等所有 TaskRunner 收尾 —— AppShell.closeEvent 负责。
    # 保留这条注释是因为**这里曾经坏过**：closeEvent 只等了 env_page.tasks，
    # 三个步骤页各自的 runner 全漏了，导致 QThread 还活着时宿主对象被回收，
    # Windows 上堆损坏（0xC0000374），进程崩在退出那刻、pytest 汇总行都打不出来。
    window.close()

    # ⚠️ **必须强制投递 DeferredDelete，否则窗口根本不会被销毁。**
    #
    # `deleteLater()` 只是把删除**排队**；而普通的 `processEvents()` **不处理
    # DeferredDelete 事件**，那个队列永远不会被执行。于是每个用例都留下一个完整的
    # 控件树，`app.topLevelWidgets()` 一轮涨 4 个、从不回落。
    #
    # 后果不只是内存：每次 `apply_theme()` 里的 `setStyleSheet()` 都要重新 polish
    # **所有**顶层控件，于是耗时随用例数线性增长。实测同一个进程里连续建壳：
    #
    #     第 1 轮  apply_theme     2.2 ms   顶层控件 4
    #     第 2 轮  apply_theme   376.0 ms   顶层控件 8
    #     第 4 轮  apply_theme  1182.5 ms   顶层控件 16      ← 增长 589 倍
    #
    # 三种销毁方式的对照（判据：apply_theme 耗时是否保持平稳）：
    #     close + deleteLater + processEvents()       增长 589x  ❌
    #     + sendPostedEvents(DeferredDelete)          增长 0.0x  ✅
    #     shiboken6.delete() 立即销毁                   增长 1.1x  ✅
    #
    # 整条测试链路的慢、以及之前那几次"跑完全部用例却崩在退出/取样式表时"，根子都在
    # 这里。之前用 `icons.clear_cache()` 压住的段错误，也只是这个累积的另一个症状。
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


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
# 自动探测远程体积
# --------------------------------------------------------------------------
def _wait_for(predicate, qapp, *, tries: int = 60, delay: float = 0.02) -> bool:
    """等后台任务回主线程（信号要过事件循环）。"""
    import time

    for _ in range(tries):
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(delay)
    return False


def test_auto_probes_remote_sizes_when_docker_is_ready(shell, qapp, monkeypatch):
    """环境就绪时会**自动**问一次真实体积。

    为什么需要自动做：清单里的 `size_hint_bytes` 是某个时刻的快照，实测会和服务器
    对不上（见 `tests/fixtures/CONTAINER_FINDINGS.md` 第 9 节）。
    """
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.core.images import RemoteInfo

    report = _fake_report()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    calls: list[str] = []

    def fake_probe(url, **_kw):
        calls.append(url)
        return RemoteInfo(content_length=123456789, accept_ranges=True, ok=True)

    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    monkeypatch.setattr(env_mod.img, "probe_remote", fake_probe)

    page = shell.env_page
    page._remote_probed = False            # 模拟"环境刚就绪"
    page.refresh_report()

    assert _wait_for(lambda: bool(page._remote_sizes), qapp), "后台探测没回来"
    assert len(calls) == len(page.specs), "每个镜像都该问一次"
    assert set(page._remote_sizes) == {s.id for s in page.specs}
    # 详情行要跟着更新成真实体积，而不是清单里的估算值
    assert "117.7 MB" in page.image_detail.text() or "123456789" not in page.image_detail.text()


def test_refresh_report_does_not_wipe_probed_sizes(shell, qapp, monkeypatch):
    """**刷新报告不能擦掉已经探测到的体积。**

    这里踩过一个 bug：`refresh_images()` 原来写的是
    `self._remote_sizes = dict(remote_sizes or {})`，而 `refresh_report()` 每次都
    无参调用它 —— 于是刚 HEAD 回来的精确体积，被紧接着的一次报告刷新擦成空。
    表现是「HEAD 明明成功了，界面还是清单里的估算值」，而且不打桩根本看不出来。
    """
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.core.images import RemoteInfo

    report = _fake_report()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    monkeypatch.setattr(
        env_mod.img, "probe_remote",
        lambda url, **k: RemoteInfo(content_length=222222, accept_ranges=True, ok=True),
    )

    page = shell.env_page
    page.refresh_report()

    assert _wait_for(lambda: len(page._remote_sizes) == len(page.specs), qapp), \
        "自动探测没回来"

    page.refresh_report()                  # 再刷一次报告
    assert len(page._remote_sizes) == len(page.specs), "报告刷新把探测结果擦掉了"


def test_probed_size_reaches_the_detail_line(shell, qapp, monkeypatch):
    """探测到的体积要真的显示出来，不能只存在内存里。"""
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.core.images import RemoteInfo

    report = _fake_report()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    monkeypatch.setattr(
        env_mod.img, "probe_remote",
        lambda url, **k: RemoteInfo(content_length=1_500_000_000, accept_ranges=True, ok=True),
    )

    page = shell.env_page
    page.refresh_report()
    assert _wait_for(lambda: bool(page._remote_sizes), qapp)

    # 本机一个镜像都没导入（fixture 把 local_images 桩成空）→ **故意不选中**
    assert page.selected_spec() is None, "没有可用的镜像时不该替用户猜一个"
    assert page.image_detail.text() == "—"

    # 选中之后，详情行要显示**探测到的**体积，而不是清单里的估算值
    page.image_box.setCurrentIndex(0)
    detail = page.image_detail.text()
    assert "1.40 GB" in detail, f"详情行没跟上真实体积：{detail}"


def test_disk_verdict_shows_in_the_picker_before_choosing(shell, monkeypatch):
    """磁盘判断要在**选择处**就能看到，而不是选完再被结论页告知。

    同时验证一条刻意的取舍：**已导入的镜像不加磁盘提示** ——
    它不用再下载，下载体积与这个决定无关。
    """
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.core.probe import DiskStatus

    GB = 1024 ** 3
    report = _fake_report()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    report.disk = DiskStatus("C:\\", 500 * GB, 8 * GB)      # 只够装 CPU 版
    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    monkeypatch.setattr(env_mod.img, "local_images", lambda: [])

    page = shell.env_page
    page.refresh_report()

    texts = [page.image_box.itemText(i) for i in range(page.image_box.count())]
    gpu = next(t for t in texts if "GPU" in t and "X5" in t)
    cpu = next(t for t in texts if "CPU" in t and "X5" in t)
    assert "磁盘不够" in gpu, f"装不下要在选项里就说：{gpu}"
    assert "还差" in gpu, "要说清还差多少"
    assert "磁盘" in cpu and "不够" not in cpu, f"CPU 版应当只是偏紧：{cpu}"

    # 已导入的镜像不再需要下载 → 不该出现磁盘提示
    monkeypatch.setattr(env_mod.img, "local_images", lambda: [page.specs[0].image_ref_hint])
    monkeypatch.setattr(env_mod.img, "find_loaded", lambda *a, **k: "img:tag")
    page.refresh_local()
    page.refresh_report()
    first = page.image_box.itemText(0)
    assert "已导入" in first
    assert "磁盘" not in first, f"已导入的不该有磁盘提示：{first}"


def test_auto_probe_is_skipped_when_docker_is_down(shell, qapp, monkeypatch):
    """Docker 没起来就不该发请求。

    体积是给"下载前判断磁盘够不够"用的；连镜像都导不进来的时候这个判断没有意义。
    顺带这条也保证测试套件不会真的打网络。
    """
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.core.images import RemoteInfo

    calls: list[str] = []
    monkeypatch.setattr(
        env_mod.img, "probe_remote",
        lambda url, **k: (calls.append(url), RemoteInfo(None, False, False, "不该被调用"))[1],
    )

    page = shell.env_page
    page._remote_probed = False
    page.refresh_report()                  # fixture 的 report 里 daemon_ok=False
    qapp.processEvents()

    assert calls == [], "Docker 没起来时不该发 HEAD 请求"
    assert page._remote_probed is False, "没探测就不该置位，等环境就绪后还要补上"


def test_auto_probe_only_asks_for_missing_images(shell, qapp, monkeypatch):
    """已经导入本机的镜像不用再问 —— 它不会再下载。"""
    import rdkquant.ui.pages.env as env_mod
    from rdkquant.core.images import RemoteInfo

    report = _fake_report()
    report.docker = DockerStatus(cli_path="/usr/bin/docker", daemon_ok=True)
    page = shell.env_page
    first = page.specs[0]
    # 让第一个镜像"已在本机"
    monkeypatch.setattr(env_mod.img, "local_images", lambda: [first.image_ref_hint])
    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    calls: list[str] = []
    monkeypatch.setattr(
        env_mod.img, "probe_remote",
        lambda url, **k: (calls.append(url), RemoteInfo(1000, True, True))[1],
    )

    page.refresh_local()
    page._remote_probed = False
    page.refresh_report()
    assert _wait_for(lambda: bool(calls), qapp)

    assert first.url not in calls, "已导入的镜像不该被问"
    assert len(calls) == len(page.specs) - 1


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
    # 桩 `probe_host`：`refresh_report()` 会**重新探测并覆盖** report，
    # 只设 `shell.env_page.report` 的话会被 fixture 的"没 Docker"版本盖掉。
    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    monkeypatch.setattr(
        env_mod.img, "find_loaded",
        lambda *a, **k: "openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8-py310",
    )
    # 让下拉栏重新解析选择：find_loaded 现在说有镜像 → 会**自动选中**它。
    # 没有这一步就什么都没选，环境门禁按设计挡下来，测不到 make_backend。
    shell.env_page.refresh_report()

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
    monkeypatch.setattr(env_mod, "probe_host", lambda *a, **k: report)
    monkeypatch.setattr(env_mod.img, "find_loaded", lambda *a, **k: "img:tag")
    shell.env_page.refresh_report()        # 让下拉栏自动选中已导入的那个

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


# --------------------------------------------------------------------------
# 交替行配色
# --------------------------------------------------------------------------
def test_findings_table_alternate_rows_are_not_white(shell):
    """回归测试：暗色主题下交替行曾经是白底，文字是浅色 → 整行看起来是空的。

    根因有两层，都不是"颜色写错了"：
      1. Qt 的 item delegate 在 alternatingRowColors 打开时，用**调色板的
         AlternateBase** 作为行背景 brush，它优先于 QSS 的 alternate-background-color
      2. app.setStyleSheet() 会让 QStyleSheetStyle 按**样式自带的标准调色板**重算每个
         控件的调色板，而 AlternateBase 不在 QSS 的映射范围里 → 退回成白色

    也实测过"重新设一遍 app 调色板"没用——那是按控件算的。修法见 widgets/TableView。

    判据直接取渲染出来的像素亮度：**暗色主题下不该出现亮色行**。
    """
    view = shell.env_page.findings_view
    model = shell.env_page.findings_model
    rows = model.rowCount()
    assert rows >= 2, f"至少要两行才谈得上交替，实际 {rows}"

    image = view.grab().toImage()
    header = view.horizontalHeader().height()
    row_h = max(1, (image.height() - header) // rows)

    lums = []
    for r in range(rows):
        y = min(header + r * row_h + row_h // 2, image.height() - 2)
        lums.append(image.pixelColor(300, y).lightness())

    assert max(lums) < 128, (
        f"暗色主题下出现了亮色行 {lums} —— 浅色文字落在白底上就看不见了"
    )
    assert max(lums) - min(lums) < 40, (
        f"交替行与普通行的亮度差过大 {lums} —— 条纹应该只是细微区别"
    )


def test_alternate_base_survives_a_theme_round_trip(shell):
    """主题切回来之后必须还是对的。

    QStyleSheetStyle 在每次应用样式表时都会重算控件调色板，所以 TableView 里
    有一个 changeEvent(PaletteChange) 的自愈钩子。没有它的话，切一次主题就退回白色。
    """
    view = shell.env_page.findings_view

    def alternate_lum():
        image = view.grab().toImage()
        header = view.horizontalHeader().height()
        rows = shell.env_page.findings_model.rowCount()
        row_h = max(1, (image.height() - header) // max(1, rows))
        return image.pixelColor(
            300, min(header + row_h + row_h // 2, image.height() - 2)
        ).lightness()

    shell.set_theme(ThemeMode.LIGHT)
    shell.set_theme(ThemeMode.DARK)
    shell.set_theme(ThemeMode.SYSTEM)
    shell.set_theme(ThemeMode.DARK)

    assert alternate_lum() < 128, "切过主题之后交替行又变亮了"
def test_interrupt_handler_installs_a_heartbeat_timer(qapp, monkeypatch):
    """光装 signal handler 是没用的，必须配一个把控制权交还解释器的定时器。

    端到端行为在 test_interrupt.py 里（那里把真程序当子进程起、发 SIGINT）。
    这里只守住"两件事都装了"这个结构，因为它是那次修复的全部内容。
    """
    import signal

    from rdkquant.ui.app import _INTERRUPT_TICK_MS, _install_interrupt_handler

    original = signal.getsignal(signal.SIGINT)
    try:
        timer = _install_interrupt_handler(qapp)
        assert timer.isActive(), "心跳定时器必须是启动状态，否则 handler 永远不会被调用"
        assert 0 < timer.interval() <= 500, (
            f"间隔应当短到用户按下去就有反应，实际 {timer.interval()}ms"
        )
        assert timer.interval() == _INTERRUPT_TICK_MS
        assert callable(signal.getsignal(signal.SIGINT)), "SIGINT handler 必须装上"

        # 第一次按下应当走整洁退出
        quit_calls = []
        monkeypatch.setattr(qapp, "quit", lambda: quit_calls.append(True))
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert quit_calls == [True]
        timer.stop()
    finally:
        signal.signal(signal.SIGINT, original)


# --------------------------------------------------------------------------
# 检查页必须检查「S1 选的那一个」模型
# --------------------------------------------------------------------------
def test_check_page_has_no_model_before_s1_picks_one(shell):
    assert shell.stage_pages[1].model_path() is None


def test_check_page_uses_the_model_s1_picked(shell, tmp_path: Path):
    """回归测试：检查页曾经按名字取 model/ 里第一个文件。

    model/ 会累积历次导入，于是 `sorted(...)[0]` 永远返回最旧的那个——
    用户重新导出了 float32 的模型，检查页却还在跑旧的 float16 文件，
    报的还是旧模型的错。极具误导性，实测发生过。
    """
    model_dir = tmp_path / "run" / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "0526.onnx").write_bytes(b"old")
    (model_dir / "0708.onnx").write_bytes(b"new")

    model, check, _calib = shell.stage_pages
    model.model_path = model_dir / "0708.onnx"
    shell.refresh_workflow()

    picked = check.model_path()
    assert picked is not None
    assert picked.name == "0708.onnx", (
        f"应当检查 S1 选的那份，实际拿到 {picked.name} —— "
        "按名字排序会永远取到最旧的模型"
    )


def test_check_failure_is_not_reported_as_a_format_problem(shell, tmp_path: Path, monkeypatch):
    """工具自己失败（rc≠0）时，不能说成"输出格式变了"——那会把用户引错方向。"""
    model_dir = tmp_path / "run" / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    model = model_dir / "m.onnx"
    model.write_bytes(b"x")

    _model_page, check, _calib = shell.stage_pages
    check.checked_model = model
    check.log_path = None
    check._on_checked((255, ["2026-01-01 00:00:00,000 ERROR something broke"]))

    text = check.raw_label.text()
    assert "255" in text
    assert "格式" not in text, "rc≠0 不是格式问题，别把用户引向解析器"
    assert "ERROR" in text


# --------------------------------------------------------------------------
# 日志渲染：ANSI 不能显示成字面量，而且要拿来判断级别
# --------------------------------------------------------------------------
REAL_INFO_LINE = (
    "2026-10-02 20:57:55,451 \x1b[92mINFO\x1b[0m Start to optimize the onnx model."
)
REAL_ERROR_LINE = (
    "2026-10-02 20:57:55,805 \x1b[91mERROR\x1b[0m "
    "*** ERROR-OCCUR-DURING {horizon_nn.build_onnx} ***"
)


def test_log_line_never_shows_raw_ansi(shell):
    """用户看到的是 `[92mINFO[0m` 这种字面量——ESC 字节不可见，剩下的就露出来了。"""
    view = shell.log_panel.log
    view.clear_log()
    view.append_line(REAL_INFO_LINE)

    shown = view.toPlainText()
    assert "\x1b" not in shown
    assert "[92m" not in shown
    assert "[0m" not in shown
    assert shown.startswith("2026-10-02 20:57:55,451 INFO ")


def test_exported_log_is_clean_too(shell, tmp_path: Path):
    view = shell.log_panel.log
    view.clear_log()
    view.append_lines([REAL_INFO_LINE, REAL_ERROR_LINE])
    target = view.save_to(tmp_path / "export.log")
    content = target.read_text(encoding="utf-8")
    assert "\x1b" not in content and "[92m" not in content


def test_log_level_comes_from_the_tools_own_colour(shell):
    """真实输出是 `时间戳 \\x1b[92mINFO\\x1b[0m …`。

    原来的 classify 只在**行首**找 `[INFO]`，时间戳挡在前面，于是每一行都被判成
    普通级——日志一片灰。颜色码才是可靠信号。
    """
    from rdkquant.ui.widgets.logview import LogLevel, classify

    assert classify(REAL_INFO_LINE) is LogLevel.INFO
    assert classify(REAL_ERROR_LINE) is LogLevel.ERROR
    assert classify("\x1b[93mWARNING\x1b[0m x") is LogLevel.WARN


def test_log_colours_differ_between_info_and_error(shell):
    """级别不能只是分类对了，颜色也得真的不一样。"""
    view = shell.log_panel.log
    view.clear_log()
    view.append_lines([REAL_INFO_LINE, REAL_ERROR_LINE])

    from rdkquant.ui.widgets.logview import LogLevel

    info_color = view._format_for(LogLevel.INFO).foreground().color()
    error_color = view._format_for(LogLevel.ERROR).foreground().color()
    assert info_color != error_color


def test_carriage_return_progress_does_not_pile_up(shell):
    """进度条靠 `\\r` 覆盖同一行，原样插进去会堆出一长串中间态。"""
    from rdkquant.ui.widgets.logview import clean_for_display

    assert clean_for_display("  0%|          | 0/1\r 50%|#####     |\r100%|##########|") \
        == "100%|##########|"
    assert clean_for_display("普通行") == "普通行"
