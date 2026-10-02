# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""把联网/耗时操作放到 Qt 线程里。

界面卡死是这类工具最常见的差评来源：下载 1.7 GB 镜像、`docker load` 解压上百层，
这些动辄几分钟。所有这类工作必须离开主线程。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from ..core.proc import Cancelled

# job(emit_line, emit_progress, cancel_event) -> result
JobFn = Callable[[Callable[[str], None], Callable[[int, int], None], threading.Event], Any]


class Task(QObject):
    line = Signal(str)
    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: JobFn) -> None:
        super().__init__()
        self._fn = fn
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = self._fn(self.line.emit, self.progress.emit, self.cancel_event)
        except Cancelled:
            self.cancelled.emit()
        except BaseException as exc:  # noqa: BLE001 - 后台线程必须兜住一切，否则静默死掉
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished.emit(result)


class TaskRunner(QObject):
    """QThread 的创建与回收。

    Task 与 QThread 都要强引用到线程真正结束为止，否则 Python 侧提前回收会
    触发 "QThread: Destroyed while thread is still running" 直接崩溃。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._alive: list[tuple[QThread, Task]] = []

    @property
    def busy(self) -> bool:
        return bool(self._alive)

    def start(self, fn: JobFn) -> Task:
        thread = QThread()
        task = Task(fn)
        task.moveToThread(thread)
        thread.started.connect(task.run)
        task.finished.connect(thread.quit)
        task.failed.connect(thread.quit)
        task.cancelled.connect(thread.quit)
        thread.finished.connect(lambda t=thread: self._reap(t))
        self._alive.append((thread, task))
        thread.start()
        return task

    def _reap(self, thread: QThread) -> None:
        self._alive = [(t, k) for (t, k) in self._alive if t is not thread]
        thread.deleteLater()

    def cancel_all(self) -> None:
        for _thread, task in list(self._alive):
            task.cancel()

    def wait_all(self, timeout_ms: int = 3000) -> None:
        for thread, task in list(self._alive):
            task.cancel()
            thread.quit()
            thread.wait(timeout_ms)
