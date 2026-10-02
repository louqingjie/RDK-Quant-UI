# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""主窗口。

第一页是「环境与镜像」——这是本项目特有的一步：RDK 的工具链镜像不是
`docker pull` 来的，而是官方离线 tar.gz，要经过「下载 → docker load → 解析真实
镜像名」三步。这三步都必须能看见进度、能取消、中断了能续传。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..core import images as img
from ..core.backends import Workspace, make_backend
from ..core.probe import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, GB, HostReport, probe_host
from .workers import TaskRunner

_LEVEL_COLOR = {
    LEVEL_OK: QColor("#1e7b3c"),
    LEVEL_WARN: QColor("#a35a00"),
    LEVEL_ERROR: QColor("#b3261e"),
}
_LEVEL_TEXT = {LEVEL_OK: "通过", LEVEL_WARN: "警告", LEVEL_ERROR: "错误"}

BOARDS = {
    "RDK X5": ("bayes-e", "x5-1.2.8-cpu", "x5-1.2.8-gpu"),
    "RDK X3": ("bernoulli2", "x3-2.6.6-cpu", "x3-2.6.6-gpu"),
}

IMAGE_HEADERS = ["ID", "板型", "架构", "变体", "离线包", "导入后预计占用", "本机", "说明"]


class TableModel(QAbstractTableModel):
    """通用只读表格模型。

    findings 与镜像列表都是「列 + 行」的形状，一个模型就够；
    用 QTableWidget 逐格塞数据会让刷新逻辑变得很脏。
    """

    def __init__(self, headers: list[str], rows=None, parent=None):
        super().__init__(parent)
        self._headers = headers
        self._rows: list[list[str]] = list(rows or [])
        self._colors: dict[tuple[int, int], QColor] = {}

    def set_rows(self, rows, colors: dict[tuple[int, int], QColor] | None = None) -> None:
        self.beginResetModel()
        self._rows = [list(map(str, r)) for r in rows]
        self._colors = colors or {}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._rows[row][col]
        if role == Qt.ItemDataRole.ForegroundRole:
            # 只给明确指定的单元格上色。整行着色会让"通过"行整片变绿，噪音太大。
            return self._colors.get((row, col))
        if role == Qt.ItemDataRole.ToolTipRole and col == len(self._headers) - 1:
            return self._rows[row][col]
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._headers[section] if orientation == Qt.Orientation.Horizontal else str(section + 1)


def _mono_font() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


def _make_table(headers: list[str], stretch_last: bool = True) -> QTableView:
    view = QTableView()
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(24)
    view.horizontalHeader().setStretchLastSection(stretch_last)
    view.setWordWrap(False)
    return view


class EnvImagePage(QWidget):
    """环境体检 + 工具链镜像获取。"""

    def __init__(self, workspace: Path, *, with_remote: bool = False, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.cache_dir = img.default_cache_dir()
        self.tasks = TaskRunner(self)
        self.specs = img.load_manifest()
        self.local: list[str] = []
        self.report: HostReport | None = None
        # 选中态必须自己记住：刷新镜像表会 reset model，Qt 会把选中行清掉。
        # 如果不在刷新后恢复选中，用户点一下行就"失去选择"，按钮随即报"请先选一行"。
        self._selected_id: str | None = None
        self._updating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ---------------- 上：环境体检 ----------------
        env_box = QGroupBox("① 环境体检")
        env_layout = QVBoxLayout(env_box)

        self.findings_model = TableModel(["级别", "说明", "下一步"])
        self.findings_view = _make_table(["级别", "说明", "下一步"])
        self.findings_view.setModel(self.findings_model)
        self.findings_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        env_layout.addWidget(self.findings_view, 1)

        env_buttons = QHBoxLayout()
        self.recheck_btn = QPushButton("重新体检")
        self.recheck_btn.clicked.connect(lambda: self.refresh_report())
        self.verdict = QLabel("")
        self.verdict.setStyleSheet("font-weight: 600;")
        env_buttons.addWidget(self.recheck_btn)
        env_buttons.addWidget(self.verdict, 1)
        env_layout.addLayout(env_buttons)
        splitter.addWidget(env_box)

        # ---------------- 中：镜像 ----------------
        img_box = QGroupBox("② 工具链镜像（官方离线包，非 docker pull）")
        img_layout = QVBoxLayout(img_box)

        self.image_model = TableModel(IMAGE_HEADERS)
        self.image_view = _make_table(IMAGE_HEADERS)
        self.image_view.setModel(self.image_model)
        self.image_view.horizontalHeader().setSectionResizeMode(
            len(IMAGE_HEADERS) - 1, QHeaderView.ResizeMode.Stretch
        )
        self.image_view.selectionModel().selectionChanged.connect(self._on_image_selected)
        img_layout.addWidget(self.image_view, 1)

        img_buttons = QHBoxLayout()
        self.fetch_btn = QPushButton("下载并导入所选镜像")
        self.fetch_btn.setDefault(True)
        self.fetch_btn.clicked.connect(self._on_fetch)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(lambda: self.tasks.cancel_all())
        self.remote_btn = QPushButton("查询远程体积")
        self.remote_btn.clicked.connect(lambda: self._probe_remote_async())
        self.cache_label = QLabel(f"离线包缓存：{self.cache_dir}")
        self.cache_label.setStyleSheet("color: #666;")
        img_buttons.addWidget(self.fetch_btn)
        img_buttons.addWidget(self.cancel_btn)
        img_buttons.addWidget(self.remote_btn)
        img_buttons.addWidget(self.cache_label, 1)
        img_layout.addLayout(img_buttons)
        splitter.addWidget(img_box)

        # ---------------- 下：日志 ----------------
        log_box = QGroupBox("③ 执行日志")
        log_layout = QVBoxLayout(log_box)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("就绪")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(_mono_font())
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self.progress)
        log_layout.addWidget(self.log, 1)
        splitter.addWidget(log_box)

        splitter.setSizes([220, 260, 260])
        root.addWidget(splitter)

        self.refresh_local()
        self.refresh_report()
        if with_remote:
            self._probe_remote_sync()

    # ------------------------------------------------------------------
    def selected_spec(self) -> img.ImageSpec | None:
        rows = self.image_view.selectionModel().selectedRows()
        if not rows:
            return None
        return self.specs[rows[0].row()]

    def refresh_local(self) -> None:
        self.local = img.local_images()

    def _image_rows(self, remote_sizes: dict[str, int] | None = None):
        rows, colors = [], {}
        for i, spec in enumerate(self.specs):
            size = (remote_sizes or {}).get(spec.id) or spec.size_hint_bytes
            present = img.find_loaded(spec.image_ref_hint, self.local)
            # 解压后的镜像层通常是压缩包的 2~2.5 倍，这个数字直接决定磁盘够不够
            footprint = img.human_bytes(int(size * 2.2)) if size else "未知"
            rows.append(
                [
                    spec.id,
                    spec.board,
                    spec.march,
                    spec.variant,
                    img.human_bytes(size) if size else "未知",
                    footprint,
                    present or "—",
                    spec.note,
                ]
            )
            if present:
                colors[(i, 6)] = _LEVEL_COLOR[LEVEL_OK]
        return rows, colors

    def refresh_images(self, remote_sizes: dict[str, int] | None = None) -> None:
        rows, colors = self._image_rows(remote_sizes)
        self._updating = True
        try:
            self.image_model.set_rows(rows, colors)
            if self._selected_id:
                for i, spec in enumerate(self.specs):
                    if spec.id == self._selected_id:
                        self.image_view.selectRow(i)
                        break
        finally:
            self._updating = False

    def refresh_report(self, required: img.ImageSpec | None = None) -> None:
        need = required.size_hint_bytes if required else None
        name = required.display_name if required else ""
        self.report = probe_host(self.workspace, required_image_bytes=need, required_image_name=name)
        rows, colors = [], {}
        for i, f in enumerate(self.report.findings):
            rows.append([_LEVEL_TEXT.get(f.level, f.level), f.message, f.hint])
            colors[(i, 0)] = _LEVEL_COLOR.get(f.level, QColor("#333"))
        self.findings_model.set_rows(rows, colors)

        ok = self.report.can_quantize
        self.verdict.setText(
            "✔ 环境就绪，可以开始量化" if ok else "✘ 环境未就绪，请按上面的提示处理"
        )
        self.verdict.setStyleSheet(
            f"font-weight:600; color:{'#1e7b3c' if ok else '#b3261e'};"
        )
        if self.report.ram.total_bytes:
            self.append_log(
                f"[体检] 内存 {self.report.ram.total_bytes / GB:.1f} GB，"
                f"建议 --shm-size={self.report.recommended_shm_bytes / GB:.1f}g"
                f"（官方示例的 15g 在 16 GB 机器上会拖死系统）"
            )
        self.refresh_images()
        for f in self.report.findings:
            if f.level == LEVEL_ERROR:
                self.append_log(f"[体检] {f.message}")

    # ------------------------------------------------------------------
    def _probe_remote_sync(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for spec in self.specs:
            info = img.probe_remote(spec.url)
            if info.ok and info.content_length:
                sizes[spec.id] = info.content_length
                self.append_log(
                    f"[远程] {spec.id:<15} {img.human_bytes(info.content_length):>10}"
                    f"  支持续传={'是' if info.accept_ranges else '否'}"
                )
            else:
                self.append_log(f"[远程] {spec.id:<15} 探测失败：{info.error}")
        self.refresh_images(sizes)
        return sizes

    def _probe_remote_async(self) -> None:
        if self.tasks.busy:
            return
        self.append_log("[远程] 正在查询各镜像的精确体积（HEAD 请求）...")

        def job(emit_line, _emit_progress, cancel):
            sizes: dict[str, int] = {}
            for spec in self.specs:
                if cancel.is_set():
                    break
                info = img.probe_remote(spec.url)
                if info.ok and info.content_length:
                    sizes[spec.id] = info.content_length
                    emit_line(f"[远程] {spec.id:<15} {img.human_bytes(info.content_length)}")
                else:
                    emit_line(f"[远程] {spec.id:<15} 探测失败：{info.error}")
            return sizes

        task = self.tasks.start(job)
        task.line.connect(self.append_log)
        task.finished.connect(lambda sizes: self.refresh_images(sizes))

    def _on_image_selected(self, *_args) -> None:
        if self._updating:
            return
        spec = self.selected_spec()
        if spec:
            self._selected_id = spec.id
            self.refresh_report(required=spec)
            self.append_log(f"[选择] {spec.display_name} → march={spec.march}")

    # ------------------------------------------------------------------
    def _on_fetch(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            self.append_log("[提示] 请先在镜像列表里选一行。")
            return
        if self.tasks.busy:
            self.append_log("[提示] 已有任务在跑，请先等待或取消。")
            return

        self.fetch_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setFormat("准备中…")
        self.append_log("")
        self.append_log("=" * 70)
        self.append_log(f"[开始] 获取镜像 {spec.display_name}")
        self.append_log(f"       这是三步流程：下载离线包 → docker load 导入 → 解析真实镜像名")
        self.append_log(f"       缓存目录 {self.cache_dir}")

        def job(emit_line, emit_progress, cancel):
            return img.ensure_image(
                spec,
                cache_dir=self.cache_dir,
                on_line=emit_line,
                on_progress=emit_progress,
                cancel=cancel,
            )

        task = self.tasks.start(job)
        task.line.connect(self.append_log)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_fetch_done)
        task.failed.connect(self._on_fetch_failed)
        task.cancelled.connect(lambda: self._on_fetch_failed("已取消（离线包已保留，可续传）"))

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        self.progress.setValue(pct)
        self.progress.setFormat(
            f"下载中 {pct}%  {img.human_bytes(done)} / {img.human_bytes(total)}  %p%"
        )

    def _on_fetch_done(self, result) -> None:
        self.fetch_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setValue(100)
        self.progress.setFormat(f"完成：{result.action}")
        self.append_log(f"[完成] {result.action} → {result.image_ref}")
        self.refresh_local()
        self.refresh_report()

    def _on_fetch_failed(self, message: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setFormat("失败")
        self.append_log(f"[失败] {message}")

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)


class QuantPage(QWidget):
    """量化配置页（骨架）。

    后端由 `make_backend()` 按环境自动选择：装了 Docker 就用真容器，
    否则退回 MockBackend——这样 UI 在没有 Docker 的机器上也能完整开发。
    """

    def __init__(self, workspace: Path, parent=None):
        super().__init__(parent)
        self.workspace = Workspace(workspace)
        self.tasks = TaskRunner(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        form_box = QGroupBox("任务配置")
        form = QFormLayout(form_box)

        self.board = QComboBox()
        self.board.addItems(list(BOARDS))
        self.board.currentTextChanged.connect(self._on_board_changed)
        form.addRow("目标板型", self.board)

        self.march = QLabel(BOARDS["RDK X5"][0])
        form.addRow("march", self.march)

        self.image_id = QLabel(BOARDS["RDK X5"][1])
        form.addRow("工具链镜像", self.image_id)

        self.backend_label = QLabel("")
        form.addRow("实际后端", self.backend_label)
        root.addWidget(form_box)

        buttons = QHBoxLayout()
        self.check_btn = QPushButton("模型检查 (hb_mapper checker)")
        self.check_btn.clicked.connect(lambda: self._run("check"))
        self.compile_btn = QPushButton("开始转换 (hb_mapper makertbin)")
        self.compile_btn.clicked.connect(lambda: self._run("compile"))
        buttons.addWidget(self.check_btn)
        buttons.addWidget(self.compile_btn)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(_mono_font())
        root.addWidget(self.log, 1)

        self._refresh_backend()

    def _on_board_changed(self, name: str) -> None:
        march, cpu_id, _gpu_id = BOARDS[name]
        self.march.setText(march)
        self.image_id.setText(cpu_id)
        self._refresh_backend()

    def _refresh_backend(self) -> None:
        board = self.board.currentText()
        march, cpu_id, gpu_id = BOARDS[board]
        spec = img.get_spec(cpu_id)
        present = img.find_loaded(spec.image_ref_hint)
        backend = make_backend(
            workspace=self.workspace,
            image=present,
            march=march,
            prefer_mock=present is None,
        )
        self.backend = backend
        if getattr(backend, "name", "") == "docker":
            self.backend_label.setText(f"Docker 容器（{present}）")
        else:
            self.backend_label.setText(
                "MockBackend（本机未就绪：未导入工具链镜像或无 Docker）—— 只产出假日志与假产物"
            )
        self.backend_label.setStyleSheet(
            "color:#1e7b3c;" if backend.name == "docker" else "color:#a35a00;"
        )

    def _run(self, action: str) -> None:
        if self.tasks.busy:
            self.append_log("[提示] 已有任务在跑。")
            return
        self.append_log("")
        self.append_log("=" * 70)
        self.append_log(f"[{action}] 后端 = {self.backend.name}")

        def job(emit_line, _emit_progress, cancel):
            if action == "check":
                return self.backend.check(
                    self.workspace.model_dir / "model.onnx", on_line=emit_line, cancel=cancel
                )
            return self.backend.compile(on_line=emit_line, cancel=cancel)

        task = self.tasks.start(job)
        task.line.connect(self.append_log)
        task.finished.connect(lambda rc: self.append_log(f"[结束] 退出码 = {rc}"))
        task.failed.connect(lambda msg: self.append_log(f"[失败] {msg}"))

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)


class MainWindow(QMainWindow):
    def __init__(self, workspace: Path, *, with_remote: bool = False):
        super().__init__()
        self.workspace = Workspace(workspace).ensure()
        self.setWindowTitle("RDK 模型量化工具")
        self.resize(1280, 820)

        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for text in ("新建任务", "打开配置", "保存配置"):
            toolbar.addAction(QAction(text, self))
        toolbar.addSeparator()
        for text in ("模型检查", "开始转换", "停止"):
            toolbar.addAction(QAction(text, self))

        tabs = QTabWidget()
        self.env_page = EnvImagePage(self.workspace.root, with_remote=with_remote)
        self.quant_page = QuantPage(self.workspace.root)
        tabs.addTab(self.env_page, "① 环境与镜像")
        tabs.addTab(self.quant_page, "② 量化配置")
        placeholder = QLabel("（逐层量化误差 / 性能对比 / 板端评测 —— 待实现）")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tabs.addTab(placeholder, "③ 结果")
        self.setCentralWidget(tabs)

        self.statusBar().showMessage(
            f"工作区 {self.workspace.root}   |   "
            f"PySide6 {__import__('PySide6').__version__}   |   "
            f"平台 {QApplication.platformName()}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        self.env_page.tasks.wait_all()
        self.quant_page.tasks.wait_all()
        super().closeEvent(event)


def run(workspace: Path | str = "./runs/current", *, screenshot: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(Path(workspace), with_remote=bool(screenshot))
    window.show()
    for _ in range(6):
        app.processEvents()

    if screenshot:
        ok = window.grab().save(screenshot)
        print(f"screenshot={screenshot} saved={ok} size={window.width()}x{window.height()}")
        window.env_page.tasks.wait_all()
        return 0 if ok else 1
    return app.exec()
