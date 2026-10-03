# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""滚轮不该改值的输入控件。

## 问题

这几个控件出厂都是 `WheelFocus` 焦点策略，而且**滚轮事件照单全收** ——
光标扫过就把取值改了。页面本身是可滚动的，用户想滚页面，结果改掉了一个筛选条件。

实测（offscreen，`QApplication.sendEvent` 送一个 `angleDelta.y = -120`）：

    QComboBox  默认              无焦点滚 0 → 1      事件 accepted=True
    QComboBox  + StrongFocus     无焦点滚 0 → 1      事件 accepted=True   ← 没用
    QComboBox  + wheelEvent忽略  无焦点滚 0 → 0      事件 ignored ✓
    QSpinBox   默认              500 → 499           accepted=True
    QDoubleSpinBox 默认          500.0 → 499.0       accepted=True
    QDoubleSpinBox + wheelEvent忽略  500.0 → 500.0   ignored ✓

**只改 `setFocusPolicy(StrongFocus)` 是不够的** —— 这一点我原本记反了，是量出来的。

## 修法

覆写 `wheelEvent` 并 `event.ignore()`。`ignore()` 不只是"不处理"，
它让事件**继续往上传**给父级滚动区 —— 所以滚轮还是能滚页面，只是不再改值。

## 用哪个

界面里**别直接 new `QComboBox` / `QSpinBox` / `QDoubleSpinBox`**，
用这里的三个。`tests/uigeom.py` 会遍历每个页面把它们揪出来。
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox


class _IgnoreWheel:
    """把滚轮让给父级。

    做成 mixin 而不是三个各写一遍：三个类的处理完全一样，
    复制三份以后改一处漏两处。`__init__` 不在这里定义，
    所以实例化时仍然走各自 Qt 基类的构造函数。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        event.ignore()


class NoWheelComboBox(_IgnoreWheel, QComboBox):
    """下拉框：滚轮不换选项。"""


class NoWheelSpinBox(_IgnoreWheel, QSpinBox):
    """整数框：滚轮不改数值。"""


class NoWheelDoubleSpinBox(_IgnoreWheel, QDoubleSpinBox):
    """浮点框：滚轮不改数值。"""
