# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""应用外壳：导航栏 + 常驻步骤条 + 页面栈 + 状态栏。

流程状态只有一个来源：`core.workflow.evaluate()`。页面自己不判断"能不能跑"，
只负责把事实写进 `RunContext`，然后发 `stateChanged`；外壳重新评估一遍，
把结果推给步骤条。这样"哪一步能做"的逻辑永远只有一份，而且能脱离 Qt 单测。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..core.workflow import STAGES, RunContext, Stage, evaluate, next_stage
from . import icons
from .metrics import METRICS, font_with_delta
from .pages import EnvPage, PlaceholderPage, ToolboxPage
from .palette import ThemeMode, apply_theme
from .widgets import LogPanel, StepItem, Stepper, StepState

STAGE_ICON = {
    Stage.ENV: "env",
    Stage.MODEL: "model",
    Stage.CHECK: "search",
    Stage.CALIB: "layers",
    Stage.CONVERT: "cpu",
    Stage.PERF: "gauge",
    Stage.ACCURACY: "target",
}

#: 每一步计划在哪个里程碑接通真实实现
STAGE_MILESTONE = {
    Stage.ENV: "M1",
    Stage.MODEL: "M3",
    Stage.CHECK: "M3",
    Stage.CALIB: "M3",
    Stage.CONVERT: "M4",
    Stage.PERF: "M5",
    Stage.ACCURACY: "M5",
}

THEME_ORDER = (ThemeMode.SYSTEM, ThemeMode.LIGHT, ThemeMode.DARK)
THEME_LABEL = {
    ThemeMode.SYSTEM: ("auto", "主题：跟随系统"),
    ThemeMode.LIGHT: ("sun", "主题：浅色"),
    ThemeMode.DARK: ("moon", "主题：深色"),
}

TOOLBOX_KEY = "toolbox"


class AppShell(QMainWindow):
    def __init__(
        self,
        workspace: Path,
        *,
        platform_palette=None,
        theme: ThemeMode = ThemeMode.SYSTEM,
        with_remote: bool = False,
    ):
        super().__init__()
        m = METRICS
        self.workspace = Path(workspace)
        self.context = RunContext()
        self.statuses = []
        self._platform_palette = platform_palette
        self._theme = theme

        self.setWindowTitle("RDK 模型量化工具")
        self.resize(1340, 920)
        self.setMinimumSize(1060, 700)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_body(), 1)
        self.setCentralWidget(central)

        self._build_log_dock()
        self._build_statusbar()
        self._register_pages(with_remote=with_remote)
        self._apply_theme_icon()

        shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        shortcut.activated.connect(self.toggle_log)

        self.nav.setCurrentRow(0)
        self.refresh_workflow()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        m = METRICS
        header = QWidget()
        layout = QVBoxLayout(header)
        layout.setContentsMargins(m.space_4, m.space_2, m.space_4, 0)
        layout.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(m.space_3)
        self.title = QLabel(self.workspace.name or str(self.workspace))
        self.title.setFont(font_with_delta(self.font(), m.font_delta_title, bold=True))
        top.addWidget(self.title)
        self.subtitle = QLabel(f"工作区  {self.workspace}")
        self.subtitle.setObjectName("Muted")
        top.addWidget(self.subtitle)
        top.addStretch(1)

        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("Ghost")
        self.theme_btn.setFixedWidth(40)
        self.theme_btn.clicked.connect(self._cycle_theme)
        top.addWidget(self.theme_btn)
        layout.addLayout(top)

        self.stepper = Stepper()
        self.stepper.set_items(
            [
                StepItem(key=spec.stage.value, title=spec.title, official_ref=spec.official_ref)
                for spec in STAGES
            ]
        )
        self.stepper.stepActivated.connect(self._on_step_activated)
        layout.addWidget(self.stepper)
        return header

    def _build_body(self) -> QWidget:
        m = METRICS
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(m.nav_width)
        self.nav.setFrameShape(QListWidget.Shape.NoFrame)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        return body

    def _build_log_dock(self) -> None:
        """日志做成全局可停靠面板。

        每一步都要看工具链原始输出，做成每页一份既浪费纵向空间，也让
        "跑到一半切页面看日志"变成不可能。做成停靠面板后切页面日志不断，
        用户还能把它关掉腾地方。
        """
        self.log_panel = LogPanel()
        dock = QDockWidget("执行日志", self)
        dock.setObjectName("LogDock")
        dock.setWidget(self.log_panel)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        # 去掉 Qt 默认的停靠标题栏（观感过时），改为由状态栏按钮控制显隐
        dock.setTitleBarWidget(QWidget())
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.log_dock = dock
        self.resizeDocks([dock], [220], Qt.Orientation.Vertical)

    def toggle_log(self) -> None:
        self.log_dock.setVisible(not self.log_dock.isVisible())
        self._sync_log_button()

    def _sync_log_button(self) -> None:
        if hasattr(self, "log_btn"):
            visible = self.log_dock.isVisible()
            self.log_btn.setText("隐藏日志" if visible else "显示日志")

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)
        self.status_note = QLabel("")
        bar.addWidget(self.status_note, 1)
        self.log_btn = QPushButton("隐藏日志")
        self.log_btn.setObjectName("Ghost")
        self.log_btn.setToolTip("Ctrl+L")
        self.log_btn.clicked.connect(self.toggle_log)
        bar.addPermanentWidget(self.log_btn)

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------
    def _register_pages(self, *, with_remote: bool) -> None:
        self.pages: dict[str, QWidget] = {}

        self.env_page = EnvPage(
            self.workspace, log_panel=self.log_panel, with_remote=with_remote
        )
        self.env_page.envChanged.connect(self.refresh_workflow)

        for spec in STAGES:
            key = spec.stage.value
            if spec.stage is Stage.ENV:
                page = self.env_page
            else:
                page = PlaceholderPage(spec, milestone=STAGE_MILESTONE.get(spec.stage, ""))
            self._add_page(key, spec.title, STAGE_ICON[spec.stage], page)

        self._add_page(TOOLBOX_KEY, "工具箱", "toolbox", ToolboxPage())

    def _add_page(self, key: str, title: str, icon_name: str, page: QWidget) -> None:
        item = QListWidgetItem(icons.icon(icon_name, size=18), title)
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setToolTip(title)
        self.nav.addItem(item)
        self.stack.addWidget(page)
        self.pages[key] = page

    def current_key(self) -> str:
        item = self.nav.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else STAGES[0].stage.value

    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            self.refresh_workflow()

    def _on_step_activated(self, key: str) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                self.nav.setCurrentRow(row)
                return

    # ------------------------------------------------------------------
    # 流程状态
    # ------------------------------------------------------------------
    def refresh_workflow(self) -> None:
        """重新评估流程并推给步骤条。这是唯一的状态更新入口。"""
        self.env_page.apply_to(self.context)

        key = self.current_key()
        current = Stage(key) if key in {s.stage.value for s in STAGES} else None
        self.statuses = evaluate(self.context, current=current)
        if current is not None:
            self.stepper.set_current(current.value)

        by_stage = {status.stage: status for status in self.statuses}
        for spec in STAGES:
            status = by_stage[spec.stage]
            reasons = status.blockers or status.warnings
            self.stepper.set_state(
                spec.stage.value,
                StepState(status.state.value),
                "\n".join(reasons),
            )
        self._update_status_note()

    def _update_status_note(self) -> None:
        stage = next_stage(self.statuses)
        if stage is None:
            self.status_note.setText("全流程已完成")
            return
        title = next(s.title for s in STAGES if s.stage is stage)
        self.status_note.setText(f"下一步：{title}")

    def blockers_for(self, stage: Stage) -> list[str]:
        for status in self.statuses:
            if status.stage is stage:
                return list(status.blockers)
        return []

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def _cycle_theme(self) -> None:
        index = THEME_ORDER.index(self._theme)
        self.set_theme(THEME_ORDER[(index + 1) % len(THEME_ORDER)])

    def set_theme(self, mode: ThemeMode) -> None:
        app = QApplication.instance()
        if app is None:
            return
        self._theme = mode
        # 图标是按颜色缓存的，换主题要清掉，否则导航栏会留着旧主题的颜色
        icons.clear_cache()
        apply_theme(app, mode, platform_palette=self._platform_palette)
        self._apply_theme_icon()
        self._reload_nav_icons()
        QSettings().setValue("ui/theme", mode.value)

    def _apply_theme_icon(self) -> None:
        name, tip = THEME_LABEL[self._theme]
        self.theme_btn.setIcon(icons.icon(name, size=18))
        self.theme_btn.setToolTip(f"{tip}（点击切换）")

    def _reload_nav_icons(self) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            key = item.data(Qt.ItemDataRole.UserRole)
            if key == TOOLBOX_KEY:
                item.setIcon(icons.icon("toolbox", size=18))
                continue
            try:
                stage = Stage(key)
            except ValueError:
                continue
            item.setIcon(icons.icon(STAGE_ICON[stage], size=18))

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        self.env_page.tasks.wait_all()
        super().closeEvent(event)


def load_theme() -> ThemeMode:
    raw = QSettings().value("ui/theme", ThemeMode.SYSTEM.value)
    try:
        return ThemeMode(str(raw))
    except ValueError:
        return ThemeMode.SYSTEM
