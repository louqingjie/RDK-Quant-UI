# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""流程页面的公共基类。

三个新页面（模型准备 / 模型检查 / 校准数据）共享同一套骨架：可滚动的主体、
结论表、把长活丢给后台线程、以及"把事实写进 RunContext 并通知 Shell"。

页面**不做门禁判断**——那是 `core/workflow.py` 的事。页面只负责产出事实。
这条边界是刻意的：门禁逻辑有 40 项测试守着，页面一旦插手就绕过了它们。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...core.findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Finding
from ...core.workflow import RunContext, StageSpec
from ..metrics import METRICS
from ..palette import semantic_colors
from ..widgets import Card, LogPanel, TableModel, fit_columns_to_content, make_table
from ..workers import TaskRunner
from .env import fit_table_height

_LEVEL_TEXT = {LEVEL_OK: "通过", LEVEL_WARN: "警告", LEVEL_ERROR: "错误"}


class StagePage(QWidget):
    """流程页面基类。子类实现 `apply_to` 与 `build`。"""

    #: 本页掌握的事实发生变化，Shell 据此重新评估流程门禁
    envChanged = Signal()

    def __init__(
        self,
        spec: StageSpec,
        workspace: Path,
        *,
        backend_provider,
        log_panel: LogPanel,
        context_provider=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.spec = spec
        self.workspace = Path(workspace)
        self.backend_provider = backend_provider
        self.log_panel = log_panel
        #: 取当前的 RunContext。页面之间不直接耦合，需要别的步骤的结论时走它
        #: （例如校准页需要 S1 推导出的 input_shape）。
        self.context_provider = context_provider or (lambda: None)
        self.tasks = TaskRunner(self)
        self._findings: list[Finding] = []
        #: 本页所有的动作按钮（`_button()` 建的）。任务跑起来时统一禁用，
        #: 免得用户点了没反应又不知道为什么。
        self._action_buttons: list[QPushButton] = []

        # 主体放在滚动区里：本项目的页面都比较长，而且大字号下 Qt 不会裁剪子控件，
        # 不滚动就会互相重叠（这个坑在环境页踩过）。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        self.body = QVBoxLayout(holder)
        self.body.setContentsMargins(
            METRICS.page_margin, METRICS.page_margin,
            METRICS.page_margin, METRICS.page_margin,
        )
        self.body.setSpacing(METRICS.space_3)
        scroll.setWidget(holder)
        outer.addWidget(scroll)

        self.build()
        self.body.addStretch(1)

    # ------------------------------------------------------------------
    # 子类要实现的部分
    # ------------------------------------------------------------------
    def build(self) -> None:
        """构建界面。子类负责调用 `add_findings_card()` 等辅助方法。"""

    def apply_to(self, ctx: RunContext) -> None:
        """把本页的事实写进流程上下文（Shell 调用）。"""

    def on_environment_changed(self) -> None:
        """环境可用性翻转时调用（Shell 调用）。用于启用/禁用动作按钮。"""

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @property
    def log(self):
        return self.log_panel.log

    @property
    def progress(self):
        return self.log_panel.progress

    def backend(self):
        """当前后端；环境没就绪时返回 None。"""
        return self.backend_provider()

    def environment_hint(self) -> str:
        """环境没就绪时的说明。**要说清楚该做什么，而不是报错。**"""
        return ("还没有可用的量化环境。请先到「环境准备」导入工具链镜像"
                "（约 1.7 GB），完成后回到这里。")

    def guard_ready(self) -> bool:
        """环境没就绪时写日志并返回 False。"""
        if self.backend() is not None:
            return True
        self.log.append_line("[提示] " + self.environment_hint())
        return False

    # ------------------------------------------------------------------
    # 结论表
    # ------------------------------------------------------------------
    def add_findings_card(self, subtitle: str = "") -> Card:
        card = Card("结论", subtitle, official_ref=self.spec.official_ref)
        self.findings_model, self.findings_view = self.make_table_with(
            ["级别", "说明", "建议"]
        )
        card.add(self.findings_view)
        self.findings_card = card
        self.body.addWidget(card)
        self.set_findings([])
        return card

    @staticmethod
    def make_table_with(headers) -> tuple[TableModel, QTableView]:
        """建一张**正确接线**的表格，返回 `(model, view)`。

        为什么要这个辅助函数：`make_table()` 的第一个参数是**表头列表**，不是模型。
        传成模型的话视图上不会挂任何模型 → `columnCount()` 为 0 →
        `fit_columns_to_content()` 直接早退 → 最后渲染出一张没有表头、没有行、
        列宽全 0 的空表格。

        这个坑 M3 的三个页面全踩了（四张表全是空的），而且**不报任何错**——
        数据在 model 里明明是对的。所以把它固化成这个函数，别再手写序对。
        """
        headers = list(headers)
        model = TableModel(headers)
        view = make_table(headers)
        view.setModel(model)
        return model, view

    def set_findings(self, findings) -> None:
        self._findings = list(findings)
        colors = semantic_colors(self.palette())
        tone_map = {
            LEVEL_OK: colors["success"],
            LEVEL_WARN: colors["warning"],
            LEVEL_ERROR: colors["danger"],
        }
        rows, cell_colors = [], {}
        for index, finding in enumerate(self._findings):
            rows.append([
                _LEVEL_TEXT.get(finding.level, finding.level),
                finding.message,
                finding.hint,
            ])
            cell_colors[(index, 0)] = tone_map.get(finding.level, colors["muted"])
        self.findings_model.set_rows(rows, cell_colors)
        fit_columns_to_content(self.findings_view)
        fit_table_height(self.findings_view, self.findings_model, min_rows=2)

    def info(self, findings) -> None:
        """写结论表并通知 Shell。"""
        self.set_findings(findings)
        self.envChanged.emit()

    def blocked(self) -> bool:
        return any(f.level == LEVEL_ERROR for f in self._findings)

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    def start(self, job, on_done, *, label: str = "") -> bool:
        """起一个后台任务；**顺手把"正在运行"这件事显示出来**。

        以前这里只负责起线程，界面完全没反应：底部进度条一直写着「就绪」，
        用户不知道在不在跑、要等多久。截图里 S1 正在 `docker run`，
        整页没有任何忙碌迹象。
        """
        if self.tasks.busy:
            return False
        if label:
            self.log.append_line(f"[{label}] 开始")
        name = label or "正在运行"
        self.log_panel.begin_busy(name)
        self._set_actions_enabled(False)

        task = self.tasks.start(job)

        def finish(result):
            self._end_busy(f"{name}完成")
            on_done(result)

        def fail(message: str):
            # ⚠️ 任务抛异常时 `finished` **不会**触发。不接这个信号的话，
            # 进度条会一直转下去，看起来像卡死——比不提示还糟。
            self.log.append_line(f"[{name}] 失败：{message}")
            self._end_busy("失败")

        def cancelled():
            self.log.append_line(f"[{name}] 已取消")
            self._end_busy("已取消")

        task.line.connect(self.log.append_line)
        task.finished.connect(finish)
        task.failed.connect(fail)
        task.cancelled.connect(cancelled)
        return True

    def _end_busy(self, text: str) -> None:
        self.log_panel.end_busy(text)
        # 先全部打开，再让页面自己按"环境就绪没有"关掉该关的 ——
        # `refresh_buttons()` 是各页面实现的，只有它知道哪些不该开。
        self._set_actions_enabled(True)
        self.refresh_buttons()

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in self._action_buttons:
            button.setEnabled(enabled)

    def refresh_buttons(self) -> None:
        """按环境就绪情况刷新按钮。子类覆盖；基类什么都不做。

        （基类必须有一个默认实现：`_end_busy()` 会无条件调用它，
        而没有动作按钮的页面不需要覆盖。）
        """

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _button(self, text: str, on_click, *, primary: bool = False,
                tooltip: str = "") -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("Primary")
        if tooltip:
            button.setToolTip(tooltip)
        button.clicked.connect(on_click)
        self._action_buttons.append(button)
        return button

    def _hint_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("Muted")
        return label

    @staticmethod
    def _table(view: QTableView, model: TableModel, *, min_rows: int = 2) -> QTableView:
        fit_columns_to_content(view)
        fit_table_height(view, model, min_rows=min_rows)
        return view
