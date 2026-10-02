# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""共享日志面板。

为什么做成全局共享而不是每个页面各带一个：几乎每一步都要看工具链的原始输出
（checker、makertbin、hb_perf、hrt_model_exec），每个页面复制一份既浪费纵向
空间，也让"跑到一半切页面看日志"变成不可能。做成外壳底部的可停靠面板之后，
切页面日志不断，而且用户能把它关掉腾出空间。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..metrics import METRICS, font_with_delta
from .logview import LogView


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        m = METRICS

        root = QVBoxLayout(self)
        root.setContentsMargins(m.space_3, m.space_2, m.space_3, m.space_3)
        root.setSpacing(m.space_2)

        head = QHBoxLayout()
        head.setSpacing(m.space_2)
        title = QLabel("执行日志")
        title.setFont(font_with_delta(self.font(), 0, bold=True))
        head.addWidget(title)
        head.addStretch(1)

        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        self.autoscroll.toggled.connect(lambda on: self.log.set_auto_scroll(on))
        head.addWidget(self.autoscroll)

        copy_btn = QPushButton("复制")
        copy_btn.setObjectName("Ghost")
        copy_btn.setToolTip("把全部日志复制到剪贴板")
        copy_btn.clicked.connect(self._copy)
        head.addWidget(copy_btn)

        save_btn = QPushButton("导出")
        save_btn.setObjectName("Ghost")
        save_btn.setToolTip("把日志保存成文本文件，便于贴给他人排查")
        save_btn.clicked.connect(self._save)
        head.addWidget(save_btn)

        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("Ghost")
        clear_btn.clicked.connect(lambda: self.log.clear_log())
        head.addWidget(clear_btn)
        root.addLayout(head)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("就绪")
        root.addWidget(self.progress)

        self.log = LogView()
        root.addWidget(self.log, 1)

        # 让工具栏按钮能用键盘焦点访问
        for button in (copy_btn, save_btn, clear_btn):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    # ------------------------------------------------------------------
    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.log.text_all())

    def _save(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "导出日志", "rdkquant.log", "文本文件 (*.log *.txt)"
        )
        if not path:
            return
        try:
            self.log.save_to(Path(path))
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
        else:
            self.log.append_line(f"[本工具] 日志已导出到 {path}")

    def set_progress(self, value: int | None, text: str = "") -> None:
        if value is None:
            self.progress.setValue(0)
        else:
            self.progress.setValue(value)
        if text:
            self.progress.setFormat(text)
