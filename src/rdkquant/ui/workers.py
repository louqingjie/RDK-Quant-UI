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


class JobCoordinator(QObject):
    """全应用**同一时刻只允许一个后台任务**（"单飞"）。

    **为什么需要**：`TaskRunner` 是**每个页面一个**，页面之间没有任何协调。
    于是可以一边在 S2 跑模型检查、一边切到 S3 跑校准数据生成 —— 两个
    `docker run` 并行。而日志面板和进度条是**全局共享**的
    （`StagePage.progress` 直接返回 `self.log_panel.progress`），
    两路输出交织成一团、进度条互相覆盖，用户根本看不懂在发生什么。
    16 GB 的机器上两个 `--shm-size 4g` 的容器也不轻松。

    **为什么不塞进 `TaskRunner`**：`TaskRunner` 管的是"起线程、回收线程"，
    单飞是**应用级策略**。两件事混在一起，以后想改任何一件都会被另一件牵制。
    """

    #: 占用状态变化。各页面据此刷新自己的按钮。
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._owner: object | None = None
        self._label = ""

    @property
    def busy(self) -> bool:
        return self._owner is not None

    @property
    def label(self) -> str:
        return self._label

    def is_owner(self, page: object) -> bool:
        return self._owner is page

    def claim(self, owner: object, label: str) -> bool:
        """占位。已经被占着就返回 `False`。

        **调用方必须把失败告诉用户** —— 静默返回 False 会让按钮看起来坏了。
        用 `blocked_message()` 拿到那句话。
        """
        if self._owner is not None:
            return False
        self._owner = owner
        self._label = label or "任务"
        self.changed.emit()
        return True

    def release(self, owner: object) -> None:
        """释放。**只释放自己占的** —— 否则一个迟到结束的任务会把
        别人正在跑的占用状态清掉，单飞就形同虚设。"""
        if self._owner is not owner:
            return
        self._owner = None
        self._label = ""
        self.changed.emit()

    def blocked_message(self) -> str:
        """被挡住时给用户看的话。**要说清是谁在跑** ——
        只说"忙"用户不知道该等什么、等多久。"""
        return f"已有任务在跑：{self._label}。等它结束，或先点「取消」。"
