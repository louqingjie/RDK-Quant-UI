# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""共享日志面板。

为什么做成全局共享而不是每个页面各带一个：几乎每一步都要看工具链的原始输出
（checker、makertbin、hb_perf、hrt_model_exec），每个页面复制一份既浪费纵向
空间，也让"跑到一半切页面看日志"变成不可能。做成外壳底部的可停靠面板之后，
切页面日志不断，而且用户能把它关掉腾出空间。
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
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


def format_duration(seconds: int) -> str:
    """把秒数说成人话。"""
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒" if rest else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None, *,
                 progress: QProgressBar | None = None):
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

        # ⚠️ **进度条不属于日志，不放进本面板的布局里。**
        #
        # 日志面板是 QDockWidget，可以被折叠 / 浮动 / 关闭；而"正在跑什么、
        # 跑了多久"必须**一直看得见**。两者放一起的话，用户一按「隐藏日志」，
        # 忙碌状态就消失了 —— 那正是最需要它的时候。
        #
        # 所以外壳把自己状态栏上的那根传进来（见 shell.py），两边共用同一个对象；
        # 不传就自建一根（独立使用 / 测试）。
        self.progress = progress if progress is not None else QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("就绪")

        #: 忙碌状态的计时器。存在的意义是**证明它还活着** ——
        #: `docker run` 起来之后可能几分钟不吐一行日志，没有这个，
        #: 用户只能盯着一个不动的界面猜"是不是卡死了"。
        self._busy_label = ""
        self._busy_since = 0.0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(500)
        self._busy_timer.timeout.connect(self._tick_busy)

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

    # ------------------------------------------------------------------
    # 忙碌状态
    # ------------------------------------------------------------------
    def begin_busy(self, label: str) -> None:
        """进入"正在运行"状态：**不确定进度条 + 已用时间**。

        为什么不是百分比：`docker run` 里的 hb_mapper 不吐结构化进度，
        我们只能从日志里猜，猜出来的百分比是假的。与其显示一个乱跳的假进度，
        不如老实显示"在跑 + 跑了多久"—— 用户至少能确认它活着。

        （`env.py` 的镜像下载是例外，那条确实有真实百分比，走 `set_progress`。）
        """
        self._busy_label = label or "正在运行"
        self._busy_since = time.monotonic()
        self.progress.setRange(0, 0)          # 0,0 = 不确定进度（跑马灯）
        self._tick_busy()
        self._busy_timer.start()

    def _tick_busy(self) -> None:
        elapsed = int(time.monotonic() - self._busy_since)
        self.progress.setFormat(f"{self._busy_label}　已用 {format_duration(elapsed)}")

    @property
    def busy(self) -> bool:
        return self._busy_timer.isActive()

    def end_busy(self, text: str = "就绪") -> None:
        self._busy_timer.stop()
        self._busy_label = ""
        self.progress.setRange(0, 100)        # 先恢复范围再设值，否则值会被 0..0 吃掉
        self.progress.setValue(0)
        self.progress.setFormat(text)
