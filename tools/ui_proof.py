# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""UI 可行性验证程序（探针，不是正式代码）。

目的：在当前开发机上验证 PySide6/Qt 能否渲染出我们规划的量化工具界面，
     并把渲染结果保存成 PNG 作为证据。

用法：
    python tools/ui_proof.py [输出图片路径]
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------
# 模拟 hb_mapper makertbin 的逐层量化误差表
# --------------------------------------------------------------------------
MOCK_LAYERS = [
    ("Conv_0", "Conv", "BPU", 0.9992, 41.2),
    ("Conv_2", "Conv", "BPU", 0.9985, 38.7),
    ("Mul_17", "Mul", "BPU", 0.9971, 35.4),
    ("Add_23", "Add", "BPU", 0.9964, 34.1),
    ("Conv_31", "Conv", "BPU", 0.9940, 31.8),
    ("Sigmoid_55", "Sigmoid", "BPU", 0.9902, 28.3),
    ("Conv_78", "Conv", "BPU", 0.9871, 26.9),
    ("Softmax_91", "Softmax", "CPU", 0.9623, 18.4),
    ("Concat_104", "Concat", "BPU", 0.9588, 16.2),
    ("Resize_118", "Resize", "CPU", 0.9401, 11.7),
]

MOCK_LOG = """\
[INFO] 正在解析配置 config.yaml
[INFO] model_parameters.march = bayes-e   (目标板型: RDK X5)
[INFO] onnx_model = yolo11n.onnx
[INFO] input_name = images, input_shape = 1x3x640x640, input_type_train = rgb/NCHW
[INFO] input_type_rt = nv12  ->  将自动插入 YUV->RGB 转换节点
[INFO] norm_type = data_mean_and_scale, mean_value = 123.675 116.28 103.53, scale_value = 0.01712
[INFO] calibration_parameters.cal_data_dir = ./cal_images  (检测到 48 张图片)
[INFO] calibration_type = default, cal_data_type = float32, per_channel = True
[INFO] ------------------------ hb_mapper checker ------------------------
[INFO] The converted model node information:
[INFO]       Node        Type      Device
[INFO]       Conv_0      Conv      BPU
[INFO]       Softmax_91  Softmax   CPU     <-- 回落到 CPU，可能影响性能
[INFO]       Resize_118  Resize    CPU
[WARN] 检测到 2 个算子运行在 CPU 上，建议检查 node_info 配置
[INFO] ------------------------ hb_mapper makertbin ---------------------
[INFO] compiler_parameters.optimize_level = O3, compile_mode = latency, jobs = 8
[INFO] 校准中 ... 48/48 samples
[INFO] 量化中 ... 生成 int8 模型
[INFO] 编译中 ... 使用 8 个进程
[SUCCESS] 转换完成
[SUCCESS] 输出: yolo11n_bayes-e_640x640_nv12.bin  (12.4 MB)
[INFO] 建议下一步: hrt_model_exec perf --model_file yolo11n_bayes-e_640x640_nv12.bin --thread_num 1
"""


class LayerTableModel(QAbstractTableModel):
    """逐层量化误差表——用 Model/View 而不是 QTableWidget。"""

    HEADERS = ["层名", "算子类型", "运行位置", "余弦相似度", "SNR (dB)"]

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self._rows = rows

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        name, op, device, cos, snr = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return name
            if col == 1:
                return op
            if col == 2:
                return device
            if col == 3:
                return f"{cos:.4f}"
            if col == 4:
                return f"{snr:.1f}"
        if role == Qt.ItemDataRole.TextAlignmentRole and col >= 3:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and col == 2 and device == "CPU":
            from PySide6.QtGui import QColor

            return QColor("#c0392b")
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return str(section + 1)


def _path_row(line_edit: QLineEdit, caption: str) -> QWidget:
    """一行：输入框 + 浏览按钮。"""
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    browse = QPushButton("浏览…")
    browse.setFixedWidth(72)

    def pick():
        if caption.startswith("目录"):
            path = QFileDialog.getExistingDirectory(box, caption)
        else:
            path, _ = QFileDialog.getOpenFileName(box, caption, "", "ONNX (*.onnx)")
        if path:
            line_edit.setText(path)

    browse.clicked.connect(pick)
    layout.addWidget(line_edit, 1)
    layout.addWidget(browse)
    return box


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RDK 模型量化工具")
        self.resize(1180, 760)

        # ---------------- 工具栏 ----------------
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for text in ("新建任务", "打开配置", "保存配置"):
            toolbar.addAction(QAction(text, self))
        toolbar.addSeparator()
        for text in ("模型检查", "开始转换", "停止"):
            toolbar.addAction(QAction(text, self))

        # ---------------- 左侧：配置表单 ----------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(10)

        basic = QGroupBox("基本设置")
        form = QFormLayout(basic)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.board = QComboBox()
        self.board.addItems(
            ["RDK X5  (march = bayes-e)", "RDK X3  (march = bernoulli2)"]
        )
        form.addRow("目标板型", self.board)

        self.onnx = QLineEdit("yolo11n.onnx")
        form.addRow("ONNX 模型", _path_row(self.onnx, "选择 ONNX 模型"))

        self.calib = QLineEdit("./cal_images")
        form.addRow("校准集目录", _path_row(self.calib, "选择校准集目录"))
        left_layout.addWidget(basic)

        io_group = QGroupBox("输入 / 预处理")
        io_form = QFormLayout(io_group)
        io_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.input_shape = QLineEdit("1x3x640x640")
        io_form.addRow("输入尺寸", self.input_shape)

        self.rt_type = QComboBox()
        self.rt_type.addItems(["nv12", "yuv444", "rgb", "bgr", "gray"])
        io_form.addRow("运行时输入格式", self.rt_type)

        self.train_type = QComboBox()
        self.train_type.addItems(["rgb", "bgr", "gray", "featuremap"])
        io_form.addRow("训练输入格式", self.train_type)

        self.norm = QComboBox()
        self.norm.addItems(
            ["data_mean_and_scale", "data_mean", "data_scale", "no_preprocess"]
        )
        io_form.addRow("norm_type", self.norm)

        self.mean = QLineEdit("123.675 116.28 103.53")
        io_form.addRow("mean_value", self.mean)

        self.scale = QLineEdit("0.01712")
        io_form.addRow("scale_value", self.scale)
        left_layout.addWidget(io_group)

        quant_group = QGroupBox("量化 / 编译")
        q_form = QFormLayout(quant_group)
        q_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.cal_type = QComboBox()
        self.cal_type.addItems(["default", "mix", "kl", "max", "load"])
        q_form.addRow("校准算法", self.cal_type)

        self.dtype = QComboBox()
        self.dtype.addItems(["int8", "int16 first"])
        q_form.addRow("量化精度", self.dtype)

        self.opt_level = QComboBox()
        self.opt_level.addItems(["O0", "O1", "O2", "O3"])
        self.opt_level.setCurrentText("O3")
        q_form.addRow("优化等级", self.opt_level)

        self.compile_mode = QComboBox()
        self.compile_mode.addItems(["latency", "bandwidth"])
        q_form.addRow("编译策略", self.compile_mode)

        self.jobs = QSpinBox()
        self.jobs.setRange(1, 32)
        self.jobs.setValue(8)
        q_form.addRow("编译进程数", self.jobs)
        left_layout.addWidget(quant_group)

        left_layout.addStretch(1)

        run_row = QHBoxLayout()
        check_btn = QPushButton("模型检查")
        run_btn = QPushButton("开始转换")
        run_btn.setDefault(True)
        stop_btn = QPushButton("停止")
        stop_btn.setEnabled(False)
        for b in (check_btn, run_btn, stop_btn):
            run_row.addWidget(b)
        left_layout.addLayout(run_row)

        # ---------------- 右侧：日志 / 误差表 ----------------
        self.tabs = QTabWidget()

        self.log = QPlainTextEdit(MOCK_LOG)
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.log.setFont(mono)
        self.tabs.addTab(self.log, "构建日志")

        self.table = QTableView()
        self.table.setModel(LayerTableModel(MOCK_LAYERS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.tabs.addTab(self.table, "逐层量化误差")

        placeholder = QLabel("  (性能对比 / 板端评测 占位)  ")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tabs.addTab(placeholder, "性能对比")

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(8)

        info = QGridLayout()
        info.addWidget(QLabel("当前任务：yolo11n → RDK X5"), 0, 0)
        info.addWidget(QLabel("状态：转换完成"), 0, 1)
        right_layout.addLayout(info)

        right_layout.addWidget(self.tabs, 1)

        self.progress = QProgressBar()
        self.progress.setValue(100)
        self.progress.setFormat("已完成  %p%")
        right_layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 760])
        self.setCentralWidget(splitter)

        self.statusBar().showMessage(
            "就绪  |  Qt {}  |  PySide6 {}  |  平台 {}".format(
                __import__("PySide6.QtCore", fromlist=["qVersion"]).qVersion(),
                __import__("PySide6").__version__,
                QApplication.platformName(),
            )
        )


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "ui_proof.png"

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    # 让布局/字体/绘制全部结算完，再抓图
    for _ in range(5):
        app.processEvents()

    pixmap = win.grab()
    ok = pixmap.save(out)
    print(f"platform   : {QApplication.platformName()}")
    print(f"window size: {win.width()}x{win.height()}, dpr={win.devicePixelRatio()}")
    print(f"screenshot : {out}  saved={ok}  size={pixmap.width()}x{pixmap.height()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
