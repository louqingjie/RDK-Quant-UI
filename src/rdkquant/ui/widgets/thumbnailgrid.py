# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""缩略图网格：可勾选，**缩略图按需生成**。

## 为什么按需

原来 `set_images()` 会把每个条目的 `QIcon` 一次建出来，于是上限只能是
`MAX_TILES = 200` —— 3504 张要先批量生成 26.5 秒、写 35 MB PNG。

但 Qt 的 `QListView` **只为可见项请求 `data()`**。实测（3504 个条目、
1000×540 的视口）：

    条目总数：3504      视口能显示：约 40 个
    data(DecorationRole) 被请求：**42 次**（1.2%）
    滚到底之后新增：32 次，最大行号 3503

所以"随滚动随生成"不是取巧，是**顺着 Qt 本来的工作方式**：请求到了才解码。

## 线程规则（三条，都踩过）

1. **`QPixmap` / `QIcon` 只能在 GUI 线程创建。** 工作线程只产出 `QImage`
   （`calibimages.load_thumbnail()` 的返回值），主线程再转 `QIcon`。
2. **数据到主线程必须走排队连接。** 信号连的是绑定方法，不是闭包 ——
   闭包会被 Qt 判成直连，处理器就跑在工作线程里（`stage.py:start()` 记过这个坑）。
3. **世代守卫。** 换筛选条件时飞行中的解码结果必须整批丢弃，
   否则会把上一个条件的图错画到这一批的格子上。

## 网格自己滚（推翻早先的决定）

早先的注释写着「网格自身不滚动：外层页面已经是滚动区，嵌套滚动会很难用」。
现在**刻意反过来**：固定高度（4 行），自己滚。

因为页面高度必须与条目数**无关** —— 3504 张会让外层滚动区变成四万多像素，
筛选控件被顶到看不见的地方。固定高度的网格里，"外层滚到控件、内层滚候选图"
反而更好用，文件选择器就是这个模式。

## 勾选框而不是"选中"

这个网格的用途是"决定哪些图片进入校准集"，是**集合**语义。而 Qt 的 selection
是"当前操作焦点"，点一张会取消上一张，两件事混在一起会让用户误操作。
勾选状态是持久的、可累加的，也和最终产物一一对应。
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QListView

from .. import calibimages
from ..metrics import METRICS

#: 网格固定显示几行。页面高度因此与条目数无关。
GRID_ROWS = 4

#: 解码好的图标最多缓存多少个。可见的只有 40 个左右，600 留足了往回滚的余量。
ICON_CACHE_MAX = 600


class _LoaderSignals(QObject):
    """把工作线程的结果带回主线程。

    **必须是独立的 QObject，而且要有强引用。** `QRunnable` 不是 `QObject`，
    发不了信号；而如果这个对象被 GC 掉，飞行中的任务就无处投递
    （`workers.py` 里记过同类教训：线程对象提前回收会直接崩）。
    """

    #: (世代, {行号: QImage}, {行号: Exposure})
    #:
    #: ⚠️ **参数类型必须是 `object`，不能写 `dict`。** 写成 `dict` 时 PySide6
    #: 无法把 Python 字典转成 C++ 类型来做**跨线程排队投递**，运行时报
    #: `Shiboken::Conversions::_pythonToCppCopy: Cannot copy-convert ... (dict) to C++`，
    #: 而且**结果静默丢失** —— 界面上就是格子一直空着，没有任何异常。
    #: （测试抓到的：icon_rows() 永远是空集。）
    loaded = Signal(int, object, object)


class _LoadJob(QRunnable):
    """在**工作线程**里解码一批缩略图。

    ⚠️ 只产出 `QImage`。`QPixmap` / `QIcon` 只能在 GUI 线程创建。
    """

    def __init__(
        self,
        signals: _LoaderSignals,
        generation: int,
        directory: Path,
        tasks: list[tuple[int, str]],
        size: int,
    ) -> None:
        super().__init__()
        self._signals = signals
        self._generation = generation
        self._directory = directory
        self._tasks = tasks
        self._size = size

    def run(self) -> None:  # noqa: D102
        images: dict[int, QImage] = {}
        exposures: dict[int, object] = {}
        for row, name in self._tasks:
            loaded = calibimages.load_thumbnail(self._directory / name, self._size)
            if loaded is None:
                continue
            images[row], exposures[row] = loaded
        try:
            self._signals.loaded.emit(self._generation, images, exposures)
        except RuntimeError:
            # 网格（连同 signals）已经先被销毁了。丢下这批结果就行，
            # 没有任何东西还等着它 —— 不该为了这个把进程带崩。
            pass


class ThumbnailModel(QAbstractListModel):
    """只存**文字和勾选状态**的模型。图标按需向外面要。

    为什么不用 `QStandardItemModel`：PySide6 里没法覆写它的 `data()`，
    而"请求到了才解码"正需要挂在那上面。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._names: list[str] = []
        self._paths: list[Path] = []
        self._checked: list[bool] = []
        self._icons: dict[int, QIcon] = {}
        #: 有人要看某个格子的图标 → 回调它（网格负责登记并异步解码）
        self.on_icon_needed: Callable[[int], None] | None = None

    # -- 只读 ---------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._names)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._names):
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            return self._names[row]
        if role == Qt.ItemDataRole.DecorationRole:
            icon = self._icons.get(row)
            if icon is not None:
                return icon
            # 没缓存 → **登记请求，先返回空图标**。Qt 会在结果到达后
            # （我们发 dataChanged）重新来问一次。
            if self.on_icon_needed is not None:
                self.on_icon_needed(row)
            return QIcon()
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if self._checked[row] else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(self._paths[row])
        return None

    def flags(self, index: QModelIndex):  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )

    def setData(  # noqa: N802
        self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        row = index.row()
        if not 0 <= row < len(self._names):
            return False
        # 归一化：Qt 传进来的可能是 CheckState，也可能是 int
        self._checked[row] = Qt.CheckState(value) == Qt.CheckState.Checked
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    # -- 内容 ---------------------------------------------------------
    def reset(self, names: list[str], paths: list[Path]) -> None:
        self.beginResetModel()
        self._names = list(names)
        self._paths = list(paths)
        self._checked = [True] * len(names)     # 默认全选：采样结果本来就是候选
        self._icons = {}
        self.endResetModel()

    def set_icon(self, row: int, icon: QIcon) -> None:
        self._icons[row] = icon
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])

    def icon_rows(self) -> set[int]:
        return set(self._icons)

    def name_at(self, row: int) -> str:
        return self._names[row]

    def is_checked(self, row: int) -> bool:
        return self._checked[row]

    def set_checked(self, row: int, checked: bool) -> None:
        self._checked[row] = checked

    def notify_checked(self, rows: Iterable[int]) -> None:
        rows = list(rows)
        if not rows:
            return
        for row in rows:
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])


class ThumbnailGrid(QListView):
    """图标模式、可勾选的文件网格。**缩略图按需解码。**"""

    #: 勾选集合发生变化
    checkedChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setIconSize(QSize(96, 96))
        self.setGridSize(QSize(116, 132))
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)          # ← 懒加载的前提：不排版也能算出滚动范围
        self.setWordWrap(True)
        self.setSpacing(METRICS.space_1)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        # ⚠️ `_sync_height()` **不能在这里调** —— 它要读 `self._model.rowCount()`，
        # 而 `self._model` 几行之后才创建。提前调就是构造期 AttributeError，
        # 也就是 `docs/qt-traps.md` 里第 4、5 条那一类"看起来没报错其实没生效"
        # 的坑的近亲：这次倒是报了错，但报了 56 个。放到 `__init__` 末尾。

        self._model = ThumbnailModel(self)
        self._model.on_icon_needed = self._request_icon
        # 用户点勾选框时，视图会走 `model.setData(CheckStateRole)` ——
        # 那是**唯一**的交互入口，得从这儿接回 `checkedChanged`
        self._model.dataChanged.connect(self._on_data_changed)
        self.setModel(self._model)

        #: 批量改勾选状态时压住信号，避免 N 次重复通知
        self._suppress = False
        #: 当前目录（解码源图用）
        self._directory: Path | None = None
        #: 世代：换一次内容就自增，用来丢弃飞行中的旧结果
        self._generation = 0
        #: 待解码的行（同一行只登记一次）
        self._pending: set[int] = set()
        self._flush_scheduled = False
        #: 解码失败的行，避免反复重试同一个坏文件
        self._failed: set[int] = set()
        #: 行号 → 曝光。只覆盖**已解码**的行（解码顺手算的）
        self._exposure: dict[int, object] = {}

        self._signals = _LoaderSignals(self)
        self._signals.loaded.connect(self._on_loaded)   # 绑定方法 → 排队回主线程
        # 自己的线程池：不跟别处抢，而且随网格一起销毁（析构会等任务收尾）
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

        # 到这儿 `self._model` 才存在（见上面那段说明）。
        # **自己滚，但高度跟着内容走**：条目少就矮一点，多了才长到 GRID_ROWS
        # 行然后内部滚动。固定 4 行的话，只有 12 张时会白空两行。
        self._sync_height()

    # ------------------------------------------------------------------
    def _sync_height(self) -> None:
        """高度 = `min(需要几行, GRID_ROWS)`。

        和早先那版的区别是**加了上限**：早先按内容无限长，3504 张会让外层
        滚动区变成四万多像素，把筛选控件顶到看不见的地方。现在超过 4 行就
        自己滚。

        下限也是需要的：只有 12 张却占 4 行高，就是白空一片。
        只在宽度变化时重算，所以不会和 `resizeEvent` 形成死循环。
        """
        grid = self.gridSize()
        per_row = max(1, self.viewport().width() // max(1, grid.width()))
        count = self._model.rowCount()
        rows = min(GRID_ROWS, max(1, -(-count // per_row)))   # 向上取整
        wanted = rows * grid.height() + 12
        if self.height() != wanted:
            self.setFixedHeight(wanted)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_height()

    # ------------------------------------------------------------------
    # 勾选状态
    # ------------------------------------------------------------------
    def checked_names(self) -> list[str]:
        """被勾选的文件名，**按网格顺序**。

        顺序很重要：它决定了生成时处理图片的顺序，也就决定了产物文件名的对照关系。
        """
        return [
            self._model.name_at(row)
            for row in range(self._model.rowCount())
            if self._model.is_checked(row)
        ]

    def all_names(self) -> list[str]:
        return [self._model.name_at(row) for row in range(self._model.rowCount())]

    def set_all_checked(self, checked: bool) -> None:
        with self._bulk():
            rows = range(self._model.rowCount())
            for row in rows:
                self._model.set_checked(row, checked)
            self._model.notify_checked(rows)

    def invert_checked(self) -> None:
        with self._bulk():
            rows = range(self._model.rowCount())
            for row in rows:
                self._model.set_checked(row, not self._model.is_checked(row))
            self._model.notify_checked(rows)

    def uncheck(self, names: Iterable[str]) -> int:
        """取消勾选指定的名字，返回实际取消了几个。"""
        wanted = set(names)
        changed = 0
        with self._bulk() as counter:
            touched = []
            for row in range(self._model.rowCount()):
                if self._model.name_at(row) in wanted and self._model.is_checked(row):
                    self._model.set_checked(row, False)
                    touched.append(row)
                    changed += 1
            self._model.notify_checked(touched)
            counter["changed"] = changed
        return changed

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def clear(self) -> None:
        with self._bulk():
            self._generation += 1
            self._pending.clear()
            self._failed.clear()
            self._exposure.clear()
            self._model.reset([], [])
        self._sync_height()

    def set_images(self, directory: Path, names: list[str] | None = None) -> int:
        """把目录（或指定文件名）下的图片装进网格，**默认全部勾选**。

        这里**只装名字**，不解码 —— 解码由 Qt 在条目变得可见时按需触发。

        **没有数量上限。** 原来截断到 `MAX_TILES = 200`，那个上限存在的唯一
        理由就是"批量生成缩略图太贵"；现在装条目是免费的。实测 3504 张
        **0.1 秒**装完、0 张解码（原来要 26.5 秒 + 35 MB PNG）。
        """
        candidates: list[Path] = []
        if directory.is_dir():
            candidates = sorted(
                p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in calibimages.IMAGE_SUFFIXES
            )
            if names:
                wanted = set(names)
                candidates = [p for p in candidates if p.name in wanted]

        with self._bulk():
            self._generation += 1
            self._pending.clear()
            self._failed.clear()
            self._exposure.clear()
            self._directory = Path(directory)
            self._model.reset(
                [p.name for p in candidates], [p for p in candidates]
            )
        self._sync_height()
        self.verticalScrollBar().setValue(0)
        return self._model.rowCount()

    # ------------------------------------------------------------------
    # 曝光（只覆盖已解码的行）
    # ------------------------------------------------------------------
    def exposure_for(self, names: Iterable[str]) -> dict[str, object]:
        """把已解码行的曝光按**文件名**返回。

        没解码到的行不在结果里 —— 调用方（「排除过曝/欠曝」）必须如实说明
        覆盖了多少张，不能假装检查过全部。
        """
        wanted = set(names)
        result: dict[str, object] = {}
        for row, exposure in self._exposure.items():
            name = self._model.name_at(row)
            if name in wanted:
                result[name] = exposure
        return result

    def decoded_rows(self) -> int:
        return len(self._exposure)

    # ------------------------------------------------------------------
    # 按需解码
    # ------------------------------------------------------------------
    def _request_icon(self, row: int) -> None:
        """某个格子要图标了。登记下来，攒一批一起解码。"""
        if row in self._failed or self._directory is None:
            return
        if row in self._pending:
            return
        self._pending.add(row)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            # 攒到本轮事件循环结束再派发：快速滚动会触发大量 data()，
            # 一个一个派任务是浪费，而且会把线程池压垮。
            QTimer.singleShot(0, self._flush_pending)

    def _flush_pending(self) -> None:
        self._flush_scheduled = False
        if not self._pending or self._directory is None:
            return
        rows = sorted(self._pending)
        self._pending.clear()
        tasks = [(row, self._model.name_at(row)) for row in rows]
        self._pool.start(_LoadJob(
            self._signals, self._generation, self._directory, tasks, self.iconSize().width(),
        ))

    def _on_loaded(self, generation: int, images: dict, exposures: dict) -> None:
        """结果回到主线程。**世代不匹配就整批丢弃。**

        换筛选条件时飞行中的解码属于上一个条件，画上去就是错图。
        """
        if generation != self._generation:
            return
        for row, image in images.items():
            if row >= self._model.rowCount():
                continue
            # QPixmap/QIcon **只能在 GUI 线程造** —— 这里是主线程，安全
            self._model.set_icon(row, QIcon(_pixmap(image)))
            self._exposure[row] = exposures.get(row)
        for row in set(images) ^ set(exposures):    # 不该发生，但别默默吞掉
            self._failed.add(row)
        self._evict()

    def _evict(self) -> None:
        """图标缓存超上限就丢最旧的。**不动 `_exposure`**（它很小，而且
        丢曝光会让「排除过曝/欠曝」的覆盖范围随滚动来回变化）。
        """
        icons = self._model._icons
        if len(icons) <= ICON_CACHE_MAX:
            return
        for row in list(icons)[: len(icons) - ICON_CACHE_MAX]:
            del icons[row]

    # ------------------------------------------------------------------
    def _on_data_changed(self, _top_left, _bottom_right, roles) -> None:
        """只有**勾选状态**变化才对外通知。

        图标到达也会发 `dataChanged`（`DecorationRole`），那跟勾选无关 ——
        不区分的话，每解码一批格子就会让界面白算一遍"已选几张"。
        """
        if self._suppress:
            return
        if roles and Qt.ItemDataRole.CheckStateRole in roles:
            self.checkedChanged.emit()

    @contextmanager
    def _bulk(self):
        """批量改动时压住 `checkedChanged`，只在最后发一次信号。

        不做这个的话，"全选 3000 张"会触发 3000 次信号，界面会重算 3000 次计数。
        """
        self._suppress = True
        counter: dict = {}
        try:
            yield counter
        finally:
            self._suppress = False
            self.checkedChanged.emit()


def _pixmap(image: QImage):
    """`QImage` → `QPixmap`。**只能在 GUI 线程调用。**"""
    from PySide6.QtGui import QPixmap

    return QPixmap.fromImage(image)
