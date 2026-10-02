# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""流程步骤条 —— 整个"严谨符合官方流程"在界面上的主要载体。

设计要点：

* **状态三重编码**（字形 + 文字 + 颜色）。只靠颜色区分状态对色盲用户不可用，
  所以每个状态都有独立字形，tooltip 里还有中文状态名。
* **颜色在 paintEvent 里从 self.palette() 现取**，不缓存。主题切换、
  系统高对比度变化会自动跟随，上层不需要发任何通知。
* **导航自由、执行受限**：被门禁挡住的步骤仍然可以点进去看缺什么，
  拦截发生在"运行"动作上，而不是阻止用户查看。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..icons import glyph_pixmap
from ..metrics import font_with_delta
from ..palette import semantic_colors


class StepState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


# 状态的中文名——tooltip 与无障碍文本都用它，保证不只靠颜色传达信息
STATE_TEXT = {
    StepState.PENDING: "未开始",
    StepState.ACTIVE: "当前步骤",
    StepState.RUNNING: "执行中",
    StepState.DONE: "已完成",
    StepState.FAILED: "失败",
    StepState.BLOCKED: "被阻塞",
    StepState.SKIPPED: "已跳过",
}

# 每个状态对应的字形（PENDING/ACTIVE 显示序号，不需要字形）
STATE_GLYPH = {
    StepState.RUNNING: "clock",
    StepState.DONE: "check",
    StepState.FAILED: "cross",
    StepState.BLOCKED: "lock",
    StepState.SKIPPED: "skip",
}


@dataclass
class StepItem:
    key: str
    title: str
    official_ref: str = ""
    state: StepState = StepState.PENDING
    reason: str = ""          # BLOCKED 缺什么 / FAILED 为什么

    def tooltip(self) -> str:
        lines = [f"{self.title} — {STATE_TEXT.get(self.state, self.state.value)}"]
        if self.official_ref:
            lines.append(f"官方依据：{self.official_ref}")
        if self.reason:
            lines.append(self.reason)
        return "\n".join(lines)


class Stepper(QWidget):
    stepActivated = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[StepItem] = []
        self._hover = -1
        self._focus = -1
        self._current_key: str | None = None
        self._glyph_cache: dict[tuple[str, str, int], object] = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------
    def set_items(self, items) -> None:
        self._items = list(items)
        self.update()

    def items(self) -> list[StepItem]:
        return list(self._items)

    def set_state(self, key: str, state: StepState, reason: str = "") -> None:
        for item in self._items:
            if item.key == key:
                item.state = state
                item.reason = reason
                self.update()
                return

    def set_current(self, key: str) -> None:
        """标记"用户正在看的这一步"。

        这和"状态"是两件事：当前步骤可能同时是 BLOCKED（缺环境）、DONE（回看）
        或 RUNNING。所以除了状态本身的呈现，当前步骤还要有独立的"你在这里"提示，
        否则用户在一堆被锁住的步骤里找不到自己。呈现方式是标题加粗 + 强调色 +
        下划线——不依赖颜色单独传达信息。
        """
        self._current_key = key
        for item in self._items:
            if item.key == key:
                if item.state in (StepState.PENDING, StepState.ACTIVE):
                    item.state = StepState.ACTIVE
            elif item.state is StepState.ACTIVE:
                item.state = StepState.PENDING
        self.update()

    @property
    def current_key(self) -> str | None:
        return self._current_key

    def index_of(self, key: str) -> int:
        for i, item in enumerate(self._items):
            if item.key == key:
                return i
        return -1

    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(480, self._needed_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, self._needed_height())

    def _line(self) -> float:
        """当前字体的一行高度。几何全部由它推导，才能跟着字体缩放走。"""
        return float(max(12, self.fontMetrics().height()))

    def _radius(self) -> float:
        return max(12.0, self._line() * 0.76)

    def _needed_height(self) -> int:
        return int(6 + self._radius() * 2 + 5 + self._line() + 6 + 4)

    # ------------------------------------------------------------------
    # 几何
    # ------------------------------------------------------------------
    def _geometry(self) -> list[tuple[QRectF, QRectF, QPointF, float]]:
        """返回每个步骤的 (圆矩形, 标题矩形, 圆心, 半径)。"""
        n = len(self._items)
        if n == 0:
            return []
        cell = self.width() / n
        radius = self._radius()
        cy = 6.0 + radius
        title_height = self._line() + 6
        result = []
        for i in range(n):
            cx = cell * (i + 0.5)
            circle = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            title = QRectF(cx - cell / 2 + 6, cy + radius + 5, cell - 12, title_height)
            result.append((circle, title, QPointF(cx, cy), radius))
        return result

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def _glyph(self, name: str, color: QColor, size: int):
        key = (name, color.name(), size)
        pixmap = self._glyph_cache.get(key)
        if pixmap is None:
            pixmap = glyph_pixmap(name, color, size)
            self._glyph_cache[key] = pixmap
        return pixmap

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._items:
            return
        palette = self.palette()
        colors = semantic_colors(palette)
        window_text = palette.color(palette.ColorRole.WindowText)
        border = palette.color(palette.ColorRole.Mid)
        muted = colors["muted"]
        # 未开始/被锁的圆环如果直接用 Mid，在深色主题里几乎看不见。
        # 往文字色方向提一点，保证"有东西在那儿"。
        dim_border = QColor(
            round(border.red() + (window_text.red() - border.red()) * 0.38),
            round(border.green() + (window_text.green() - border.green()) * 0.38),
            round(border.blue() + (window_text.blue() - border.blue()) * 0.38),
        )

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        geometry = self._geometry()

        # ---- 连接线（先画，压在圆下面）----
        for i in range(len(geometry) - 1):
            _c1, _t1, center1, r1 = geometry[i]
            _c2, _t2, center2, r2 = geometry[i + 1]
            done = self._items[i].state is StepState.DONE
            pen = QPen(colors["success"] if done else border)
            pen.setWidthF(2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(center1.x() + r1 + 6, center1.y()),
                QPointF(center2.x() - r2 - 6, center2.y()),
            )

        # ---- 圆与字形 ----
        for i, (item, (circle, title_rect, center, radius)) in enumerate(
            zip(self._items, geometry)
        ):
            state = item.state
            filled = None
            ring = None
            glyph_color = None
            dashed = False

            if state is StepState.DONE:
                filled, glyph_color = colors["success"], palette.color(
                    palette.ColorRole.HighlightedText
                )
            elif state is StepState.FAILED:
                filled, glyph_color = colors["danger"], palette.color(
                    palette.ColorRole.HighlightedText
                )
            elif state is StepState.RUNNING:
                filled, glyph_color = colors["accent"], colors["on_accent"]
            elif state is StepState.ACTIVE:
                ring = colors["accent"]
            elif state is StepState.BLOCKED:
                ring, glyph_color, dashed = dim_border, muted, True
            elif state is StepState.SKIPPED:
                ring, glyph_color, dashed = dim_border, muted, True
            else:  # PENDING
                ring, glyph_color = dim_border, muted

            if filled is not None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(filled)
                painter.drawEllipse(circle)
            else:
                pen = QPen(ring)
                pen.setWidthF(2.2 if state is StepState.ACTIVE else 1.4)
                if dashed:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(circle)

            glyph = STATE_GLYPH.get(state)
            if glyph:
                pixmap = self._glyph(glyph, glyph_color, max(11, int(radius * 1.05)))
                half = pixmap.width() / pixmap.devicePixelRatio() / 2.0
                painter.drawPixmap(
                    QPointF(center.x() - half, center.y() - half), pixmap
                )
            else:
                painter.setPen(colors["accent"] if state is StepState.ACTIVE else muted)
                font = QFont(self.font())
                font.setBold(True)
                font.setPointSizeF(max(6.0, self.font().pointSizeF() - 0.5))
                painter.setFont(font)
                painter.drawText(
                    circle, int(Qt.AlignmentFlag.AlignCenter), str(i + 1)
                )

            # ---- 标题 ----
            is_current = item.key == self._current_key
            is_dim = state in (StepState.PENDING, StepState.BLOCKED, StepState.SKIPPED)
            if is_current:
                title_color = colors["accent"]
            else:
                title_color = muted if is_dim else window_text
            font = font_with_delta(
                self.font(), 0, bold=is_current or state in (StepState.ACTIVE, StepState.RUNNING)
            )
            painter.setFont(font)
            painter.setPen(title_color)
            metrics = painter.fontMetrics()
            text = metrics.elidedText(
                item.title, Qt.TextElideMode.ElideRight, int(title_rect.width())
            )
            painter.drawText(title_rect, int(Qt.AlignmentFlag.AlignHCenter), text)

            # 当前步骤加一条下划线——不靠颜色单独传达"你在这里"
            if is_current:
                width = min(metrics.horizontalAdvance(text) + 10, title_rect.width())
                pen = QPen(colors["accent"])
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                y = title_rect.bottom() - 1
                painter.drawLine(
                    QPointF(title_rect.center().x() - width / 2, y),
                    QPointF(title_rect.center().x() + width / 2, y),
                )

            # 焦点环（键盘可达性）
            if i == self._focus and self.hasFocus():
                pen = QPen(colors["accent"])
                pen.setWidthF(1.0)
                pen.setStyle(Qt.PenStyle.DotLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                ring_rect = circle.adjusted(-4, -4, 4, 4)
                path = QPainterPath()
                path.addRoundedRect(
                    QRectF(title_rect.left() - 4, ring_rect.top(),
                           title_rect.width() + 8,
                           (title_rect.bottom() - ring_rect.top()) + 4),
                    6, 6,
                )
                painter.drawPath(path)

        painter.end()

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _index_at(self, pos) -> int:
        for i, (circle, title, _center, _r) in enumerate(self._geometry()):
            # 命中区覆盖"圆 + 标题"整列，不要只认文字那一行
            hit = QRectF(title)
            hit.setTop(circle.top() - 2)
            if hit.contains(QPointF(pos)):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(event.position())
        if index != self._hover:
            self._hover = index
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if index >= 0
                else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position())
            if index >= 0:
                self._focus = index
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.stepActivated.emit(self._items[index].key)
                self.update()
                return
        super().mousePressEvent(event)

    def event(self, event) -> bool:
        # 按位置给 tooltip，比给整个控件设一个 tooltip 有用得多
        if event.type() == QEvent.Type.ToolTip:
            index = self._index_at(event.pos())
            if index >= 0:
                self.setToolTip(self._items[index].tooltip())
            else:
                self.setToolTip("")
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._items:
            return
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = -1 if key == Qt.Key.Key_Left else 1
            self._focus = max(0, min(len(self._items) - 1, self._focus + step))
            self.setToolTip(self._items[self._focus].tooltip())
            self.update()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if 0 <= self._focus < len(self._items):
                self.stepActivated.emit(self._items[self._focus].key)
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        if self._focus < 0 and self._items:
            active = next(
                (i for i, it in enumerate(self._items) if it.state is StepState.ACTIVE), 0
            )
            self._focus = active
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(event)
