# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""S3 校准数据准备（官方 4.1.1.5）。

职责划分见 `core/calib.py`：容器里的探针只报事实，依赖配置的策略在那边——
页面负责把用户的选择传过去。

关于生成二进制校准数据，有一条**实测**得到的硬约束：产物必须是**无文件头的
裸字节**，长度恰好 `W×H×C×itemsize`。放 `.npy` 会失败——工具按裸字节读，
多出来的 128 字节文件头会被算进元素个数。详见 tests/fixtures/CONTAINER_FINDINGS.md。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import containerops
from ...core.calib import (
    CalibBuildSpec,
    evaluate_probe,
    expected_hw_from_shape,
)
from ...core.calibfilter import (
    CalibStat,
    FilterSpec,
    apply_filter,
    duplicate_names,
    filter_and_sample,
    load_stats,
    most_common_resolution,
)
from ...core.findings import LEVEL_ERROR, LEVEL_OK, Finding
from ...core.workflow import RunContext
from ..metrics import METRICS
from ..widgets import Card, CollapsibleSection, MetricTile, Tone
from ..widgets.thumbnailgrid import MAX_TILES, ThumbnailGrid
from .stage import StagePage

CALIBRATION_TYPES = [
    ("default", "default —— 自动搜索（推荐）"),
    ("mix", "mix —— 混合校准"),
    ("kl", "kl —— KL 散度"),
    ("max", "max —— 最大值"),
    ("load", "load —— 复用已有校准参数（不需要数据）"),
    ("skip", "skip —— 跳过校准（不需要数据）"),
]
CAL_DATA_TYPES = [("float32", "float32"), ("uint8", "uint8")]
THUMB_SUBDIR = ".rdkquant/thumbs"

#: 认得的图片后缀。容器里的探针有一份同样的清单；这里这份是**宿主侧**用的
#: （决定把哪些文件收进工作区），不依赖容器就能跑。
IMAGE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
    ".ppm", ".pgm", ".pbm", ".gif",
)


class CalibPage(StagePage):
    """校准数据准备页。"""

    def build(self) -> None:
        self.directory = self.workspace / "cal_images"
        self.payload: dict | None = None
        self.verdict = None
        #: 扫描时把图片收进工作区后的那条路径（容器能看到的就是它）。
        #: **生成必须用它，不能用 self.directory** —— 那是用户选的原始路径，
        #: 可能在工作区外，容器根本看不到（实测踩过）。
        self._staged: Path | None = None
        #: 逐张统计（容器扫描时写在工作区里，宿主读来做筛选）
        self._stats: list[CalibStat] = []
        #: 内容重复的配对，来自扫描回包，形状 [[保留的, 重复的], ...]
        self._duplicates: list = []
        #: 当前网格里显示的那批名字。用来判断"筛选条件改过但没刷新预览"
        self._displayed: list[str] = []
        #: 界面是否已经搭完。**构造期间控件就会发 valueChanged**（setRange/setValue），
        #: 那时 grid / funnel_label 还不存在，回调里碰它们会 AttributeError。
        self._ready = False

        # ---- 目录 ----
        card = Card("校准数据目录", official_ref=self.spec.official_ref)
        self.dir_label = QLabel(str(self.directory))
        self.dir_label.setWordWrap(True)
        card.add(self.dir_label)

        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.space_2)
        self.pick_btn = self._button("选择目录…", self._on_pick)
        self.scan_btn = self._button("扫描", self._on_scan, primary=True)
        line.addWidget(self.pick_btn)
        line.addWidget(self.scan_btn)
        line.addStretch(1)
        card.add(row)
        card.add(self._hint_label(
            "官方建议从测试集里挑 20~100 张有代表性的图片；"
            "排除过曝、欠曝、模糊、纯黑、纯白的场景。"
        ))
        self.body.addWidget(card)

        # ---- 概览 ----
        overview = Card("扫描结果")
        tiles = QWidget()
        tiles_line = QHBoxLayout(tiles)
        tiles_line.setContentsMargins(0, 0, 0, 0)
        tiles_line.setSpacing(METRICS.space_3)
        self.tile_count = MetricTile("样本数")
        self.tile_res = MetricTile("分辨率")
        self.tile_dup = MetricTile("重复")
        self.tile_exposure = MetricTile("曝光均值范围")
        for tile in (self.tile_count, self.tile_res,
                     self.tile_dup, self.tile_exposure):
            tiles_line.addWidget(tile)
        overview.add(tiles)
        self.backend_label = self._hint_label("尚未扫描。")
        overview.add(self.backend_label)
        self.body.addWidget(overview)

        # ---- 筛选 + 缩略图 ----
        thumbs_card = Card("筛选与挑选", "缩略图由容器生成")

        # 筛选条件收进可折叠区：条件多，但多数时候用户只用其中一两个
        self.filter_section = CollapsibleSection("筛选条件", expanded=False)
        filter_body = QWidget()
        form = QGridLayout(filter_body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(METRICS.space_3)
        form.setVerticalSpacing(METRICS.space_2)

        self.width_min, self.width_max = self._range_row(
            form, 0, "宽度 (px)", 0, 100000)
        self.height_min, self.height_max = self._range_row(
            form, 1, "高度 (px)", 0, 100000)
        self.aspect_min, self.aspect_max = self._range_row(
            form, 2, "长宽比 (宽/高)", 0.0, 20.0, decimals=2, step=0.1)
        self.size_min, self.size_max = self._range_row(
            form, 3, "文件大小 (MB)", 0.0, 10000.0, decimals=2, step=0.5)

        self.mode_edit = QLineEdit()
        self.mode_edit.setPlaceholderText("不限")
        self.mode_edit.setToolTip("逗号分隔，例如 RGB,L。留空表示不限。")
        self.mode_edit.textChanged.connect(self._on_filter_changed)
        form.addWidget(QLabel("色彩模式"), 4, 0)
        form.addWidget(self.mode_edit, 4, 1, 1, 3)

        self.dup_box = QCheckBox("排除内容重复的图片")
        self.dup_box.stateChanged.connect(self._on_filter_changed)
        form.addWidget(self.dup_box, 5, 0, 1, 4)

        self.filter_section.add(filter_body)

        preset_row = QWidget()
        presets = QHBoxLayout(preset_row)
        presets.setContentsMargins(0, 0, 0, 0)
        presets.setSpacing(METRICS.space_2)
        for text, slot in (
            ("只保留最常见分辨率", self._apply_common_resolution),
            ("重置筛选", self._reset_filter),
        ):
            presets.addWidget(self._button(text, slot))
        presets.addStretch(1)
        self.filter_section.add(preset_row)
        thumbs_card.add(self.filter_section)

        # 漏斗计数：一眼看清每一级剩多少
        self.funnel_label = self._hint_label("尚未扫描。")
        thumbs_card.add(self.funnel_label)

        pick_row = QWidget()
        picks = QHBoxLayout(pick_row)
        picks.setContentsMargins(0, 0, 0, 0)
        picks.setSpacing(METRICS.space_2)
        for text, slot in (
            ("全选", lambda: self.grid.set_all_checked(True)),
            ("全不选", lambda: self.grid.set_all_checked(False)),
            # 必须包成 lambda：直接写 `self.grid.invert_checked` 会**立即求值**，
            # 而 self.grid 在下面几行才创建 → AttributeError（实测踩过）
            ("反选", lambda: self.grid.invert_checked()),
            ("排除重复", self._exclude_duplicates),
        ):
            picks.addWidget(self._button(text, slot))
        picks.addStretch(1)
        self.apply_btn = self._button(
            "应用筛选并刷新预览", self._on_apply_filter, primary=True,
            tooltip="筛选在本地即时完成；这一步是用结果重新生成缩略图，需要跑一次容器。",
        )
        picks.addWidget(self.apply_btn)
        thumbs_card.add(pick_row)

        self.grid = ThumbnailGrid()
        self.grid.checkedChanged.connect(self._on_checked_changed)
        thumbs_card.add(self.grid)
        self.body.addWidget(thumbs_card)

        # ---- 参数 ----
        params = Card("校准参数", "这些值会写进第 4 步的 config.yaml")
        self.type_box = QComboBox()
        for value, label in CALIBRATION_TYPES:
            self.type_box.addItem(label, value)
        self.type_box.currentIndexChanged.connect(self._on_param_changed)
        params.add(self._labelled("校准方式 calibration_type", self.type_box))

        self.dtype_box = QComboBox()
        for value, label in CAL_DATA_TYPES:
            self.dtype_box.addItem(label, value)
        self.dtype_box.currentIndexChanged.connect(self._on_param_changed)
        params.add(self._labelled("数据类型 cal_data_type", self.dtype_box))

        # 采样张数：官方建议 20~100 张，而真实数据集动辄几千张（实测一个装甲板数据集
        # 有 3504 张、1474 种分辨率），必须能挑出有代表性的子集。
        self.count_box = QSpinBox()
        # 上限就是网格能显示的张数：超过它就无法逐个勾选，是个隐藏的坑。
        # 官方建议 20~100，200 已经够宽。
        self.count_box.setRange(0, MAX_TILES)
        self.count_box.setValue(50)
        self.count_box.setSpecialValueText("全部")
        self.count_box.setSuffix(" 张")
        self.count_box.setToolTip(
            "从源目录里**均匀**取样多少张（0 = 全部用上）。\n"
            "均匀取样而不是随机：结果可复现，而且数据集通常按时间/场景排序，\n"
            "间隔取样天然覆盖各个片段。"
        )
        self.count_box.valueChanged.connect(self._on_param_changed)
        params.add(self._labelled("采样张数", self.count_box))

        self.preprocess_box = QCheckBox(
            "preprocess_on —— 让工具自己 resize（官方建议保持关闭）"
        )
        self.preprocess_box.stateChanged.connect(self._on_param_changed)
        params.add(self.preprocess_box)
        params.add(self._hint_label(
            "关闭 preprocess_on 时，校准数据必须已经是模型输入尺寸，"
            "工具不会替你缩放。"
        ))
        self.body.addWidget(params)

        # ---- 生成 ----
        build_card = Card("生成二进制校准数据")
        build_card.add(self._hint_label(
            "产物是给 hb_mapper 直接读的裸二进制（没有文件头）。默认**不做归一化**"
            "——官方把 norm_type 的预处理插进模型里，校准数据应当是训练格式的原始像素。"
        ))
        build_row = QWidget()
        build_line = QHBoxLayout(build_row)
        build_line.setContentsMargins(0, 0, 0, 0)
        build_line.setSpacing(METRICS.space_2)
        self.build_btn = self._button("生成校准数据", self._on_build, primary=True)
        build_line.addWidget(self.build_btn)
        build_line.addStretch(1)
        build_card.add(build_row)
        self.build_label = self._hint_label("")
        build_card.add(self.build_label)
        self.body.addWidget(build_card)

        self.add_findings_card()
        self._ready = True
        self.refresh_buttons()

    @staticmethod
    def _labelled(text: str, widget: QWidget) -> QWidget:
        holder = QWidget()
        line = QVBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.space_1)
        label = QLabel(text)
        label.setObjectName("Muted")
        line.addWidget(label)
        line.addWidget(widget)
        return holder

    # ------------------------------------------------------------------
    # 筛选
    # ------------------------------------------------------------------
    def _range_row(self, form, row: int, label: str, low, high,
                   *, decimals: int = 0, step: float = 1.0):
        """两栏的"最小值 ~ 最大值"输入。

        **0 表示不限**，不额外加"启用"复选框：少一个控件，也少一种状态组合。
        对宽高/大小/比例来说 0 都不是有效下限，所以这个约定没有歧义。
        """
        def make():
            if decimals:
                box = QDoubleSpinBox()
                box.setDecimals(decimals)
                box.setSingleStep(step)
            else:
                box = QSpinBox()
            box.setRange(low, high)
            box.setSpecialValueText("不限")
            box.valueChanged.connect(self._on_filter_changed)
            return box

        form.addWidget(QLabel(label), row, 0)
        box_min, box_max = make(), make()
        form.addWidget(box_min, row, 1)
        form.addWidget(QLabel("~"), row, 2)
        form.addWidget(box_max, row, 3)
        return box_min, box_max

    def filter_spec(self) -> FilterSpec:
        """从控件读出筛选条件。0 / 空 → None（不限）。"""
        def limit(box):
            value = box.value()
            return value or None

        return FilterSpec(
            min_width=limit(self.width_min), max_width=limit(self.width_max),
            min_height=limit(self.height_min), max_height=limit(self.height_max),
            min_aspect=limit(self.aspect_min), max_aspect=limit(self.aspect_max),
            min_bytes=int(limit(self.size_min) * 1024 * 1024) if limit(self.size_min) else None,
            max_bytes=int(limit(self.size_max) * 1024 * 1024) if limit(self.size_max) else None,
            modes=tuple(
                m.strip() for m in self.mode_edit.text().split(",") if m.strip()
            ),
            exclude_duplicates=self.dup_box.isChecked(),
        )

    def filtered_stats(self) -> list[CalibStat]:
        return list(apply_filter(
            self._stats, self.filter_spec(), duplicates=self._duplicates
        ).kept)

    def _display_names(self) -> list[str]:
        """筛选 + 采样之后，当前应当出现在网格里的名字。"""
        result, names = filter_and_sample(
            self._stats, self.filter_spec(),
            count=self.count_box.value(), duplicates=self._duplicates,
        )
        return names

    def _on_filter_changed(self) -> None:
        """筛选条件变了：**只重算计数，不碰容器**。

        计数是纯宿主机计算（读统计文件），所以能做到即时——用户拖一下数值就想
        看到"3504 → 412"，为此跑一趟容器是不可接受的。
        """
        if not self._ready:
            return
        self._refresh_funnel()
        self.envChanged.emit()

    def _refresh_funnel(self) -> None:
        if not self._stats:
            self.funnel_label.setText("尚未扫描。")
            return
        result, names = filter_and_sample(
            self._stats, self.filter_spec(),
            count=self.count_box.value(), duplicates=self._duplicates,
        )
        total = len(self._stats)
        kept = len(result.kept)
        checked = len(self.grid.checked_names())
        parts = [f"共 {total} 张"]
        if self.filter_spec().active:
            parts.append(f"筛选后 {kept}")
            why = result.explain()
            if why:
                parts.append(f"（筛掉：{why}）")
        parts.append(f"采样 {len(names)}")
        if self._displayed:
            parts.append(f"已选 {checked}")
        self.funnel_label.setText(" → ".join(parts))

        if kept == 0 and self.filter_spec().active:
            self.funnel_label.setText(
                f"共 {total} 张 → **筛选后 0 张**（筛掉：{result.explain()}）"
                "。放宽条件试试。"
            )
        if self._displayed and set(names) != set(self._displayed):
            self.funnel_label.setText(
                self.funnel_label.text() + "　⚠️ 条件已改，请点「应用筛选并刷新预览」"
            )

    def _apply_common_resolution(self) -> None:
        """一键锁定出现最多的那种分辨率。

        对"1474 种分辨率"的数据集这是最省事的收窄方式——让用户自己试数值很痛苦。
        """
        if not self._stats:
            self.log.append_line("[筛选] 请先扫描。")
            return
        common = most_common_resolution(self._stats)
        if common is None:
            self.log.append_line("[筛选] 没有可用的分辨率信息。")
            return
        width, height = common
        for box, value in ((self.width_min, width), (self.width_max, width),
                           (self.height_min, height), (self.height_max, height)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)
        self.log.append_line(f"[筛选] 已锁定最常见分辨率 {width}x{height}")
        self._on_filter_changed()

    def _reset_filter(self) -> None:
        for box in (self.width_min, self.width_max, self.height_min, self.height_max,
                    self.aspect_min, self.aspect_max, self.size_min, self.size_max):
            box.blockSignals(True)
            box.setValue(box.minimum())
            box.blockSignals(False)
        self.mode_edit.blockSignals(True)
        self.mode_edit.clear()
        self.mode_edit.blockSignals(False)
        self.dup_box.blockSignals(True)
        self.dup_box.setChecked(False)
        self.dup_box.blockSignals(False)
        self.log.append_line("[筛选] 已重置")
        self._on_filter_changed()

    def _exclude_duplicates(self) -> None:
        """把内容重复的那些取消勾选。

        用扫描回包里的 duplicates（形状 [[保留的, 重复的], ...]），
        **作用在网格上而不是筛选条件上**：用户点这个按钮的意图是"把选中的里面
        重复的去掉"，而不是"永久改变筛选口径"。
        """
        names = duplicate_names(self._duplicates)
        if not names:
            self.log.append_line("[筛选] 扫描结果里没有重复项。")
            return
        removed = self.grid.uncheck(names)
        self.log.append_line(
            f"[筛选] 检测到 {len(names)} 张重复，取消了其中 {removed} 张的勾选"
            "（不在当前预览里的重复项不受影响）"
        )

    def _on_checked_changed(self) -> None:
        if not self._ready:
            return
        self._refresh_funnel()
        self.envChanged.emit()

    def _on_apply_filter(self) -> None:
        """用当前筛选 + 采样结果重新生成缩略图。

        这是唯一需要跑容器的环节——筛选本身是纯本地的。
        """
        backend = self.backend()
        if backend is None:
            self.log.append_line("[提示] " + self.environment_hint())
            return
        if not self._stats:
            self.log.append_line("[筛选] 请先扫描。")
            return
        staged = self._staged or (self.workspace / "cal_images")
        names = self._display_names()
        if not names:
            self.log.append_line("[筛选] 当前条件下没有图片，预览已清空。")
            self.grid.clear()
            self._displayed = []
            self._refresh_funnel()
            return

        thumbs = self.workspace / THUMB_SUBDIR
        self.log.append_line(f"[筛选] 用 {len(names)} 张重新生成预览…")

        def job(emit_line, _emit_progress, cancel):
            return backend.thumbnails(
                staged, thumbs, only=names, on_line=emit_line, cancel=cancel
            )

        def done(payload):
            if not payload.get("ok"):
                self.log.append_line(f"[筛选] 预览生成失败：{payload.get('error')}")
                return
            self._displayed = list(names)
            # 只显示这批名字对应的缩略图（thumbs 目录里可能还留着上一次的）
            wanted = {Path(n).stem + ".png" for n in names}
            self.grid.set_images(thumbs, names=sorted(wanted))
            self._refresh_funnel()
            self.envChanged.emit()

        self.start(job, done, label="筛选")

    # ------------------------------------------------------------------
    def refresh_buttons(self) -> None:
        ready = self.backend() is not None
        self.pick_btn.setEnabled(ready)
        self.scan_btn.setEnabled(ready)
        self.build_btn.setEnabled(ready)
        self.apply_btn.setEnabled(ready)
        if not ready:
            self.backend_label.setText(self.environment_hint())

    def on_environment_changed(self) -> None:
        self.refresh_buttons()

    def calibration_type(self) -> str:
        return self.type_box.currentData()

    def _on_pick(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择校准数据目录", str(self.directory.parent)
        )
        if not path:
            return
        self.directory = Path(path)
        self.dir_label.setText(str(self.directory))
        self._on_scan()

    def _on_param_changed(self) -> None:
        # 改了参数就重新判定一遍——严重程度取决于 preprocess_on，
        # 用户在选项之间切换时结论必须跟着变，不能停在旧结论上。
        if self.payload is not None:
            self._evaluate()
        self.envChanged.emit()

    # ------------------------------------------------------------------
    def _stage(self, source: Path, emit_line, emit_progress, cancel) -> Path | None:
        """把校准图片收进工作区，返回容器能看到的那条路径。

        **为什么必须收进来**：容器只挂载工作区（`/workspace`）。工作区**之外**的目录
        容器根本看不到 —— 之前直接把 `C:\\Users\\...` 传给探针，容器里报的是
        "目录不存在"（实测）。这和 S1 把模型复制进 `model/` 是同一个道理。

        已经在工作区里的就不用动。
        """
        source = Path(source)
        if not source.is_dir():
            emit_line(f"[校准] 目录不存在：{source}")
            return None
        try:
            source.resolve().relative_to(self.workspace.resolve())
            return source                      # 已经在工作区里
        except ValueError:
            pass

        target = self.workspace / "cal_images"
        target.mkdir(parents=True, exist_ok=True)
        pending = [
            p for p in sorted(source.iterdir())
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        ]
        if not pending:
            emit_line(f"[校准] {source} 里没有可识别的图片")
            return None

        emit_line(f"[校准] 正在把 {len(pending)} 张图片收进工作区（cal_images/）…")
        copied = 0
        for index, path in enumerate(pending, start=1):
            if cancel.is_set():
                emit_line("[校准] 已取消")
                return None
            dest = target / path.name
            # 同名同大小就跳过，重复扫描不会反复复制
            if not (dest.exists() and dest.stat().st_size == path.stat().st_size):
                shutil.copy2(path, dest)
            copied += 1
            emit_progress(int(index * 100 / len(pending)), f"复制 {index}/{len(pending)}")
        emit_line(f"[校准] 已收进工作区：{copied} 张 → cal_images/")
        return target

    def _on_scan(self) -> None:
        backend = self.backend()
        if backend is None:
            self.log.append_line("[提示] " + self.environment_hint())
            return
        source = Path(self.directory)
        shape = self._suggested("input_shape")
        dtype = self.dtype_box.currentData()

        def job(emit_line, emit_progress, cancel):
            staged = self._stage(source, emit_line, emit_progress, cancel)
            if staged is None:
                return {"ok": False, "error": f"目录不可用：{source}"}
            payload = backend.scan_calib(
                staged, shape=shape, dtype=dtype, want_stats=True,
                on_line=emit_line, cancel=cancel,
            )
            thumbs = self.workspace / THUMB_SUBDIR
            try:
                backend.thumbnails(
                    staged, thumbs, count=self.count_box.value(),
                    on_line=emit_line, cancel=cancel,
                )
            except Exception as exc:      # 缩略图失败不该毁掉整次扫描
                emit_line(f"[校准] 缩略图生成失败：{exc}")
            payload["_staged"] = str(staged)
            return payload

        self.start(job, self._on_scanned, label="校准")

    def _suggested(self, key: str) -> str | None:
        """读 S1 推导出来的参数。页面之间不直接耦合，走 RunContext。"""
        ctx = self.context_provider()
        if ctx is None:
            return None
        return (ctx.suggested_input_parameters or {}).get(key) or None

    def _on_scanned(self, payload: dict) -> None:
        self.payload = payload
        staged = payload.get("_staged")
        if staged:
            self._staged = Path(staged)
        self._duplicates = list(payload.get("duplicates") or [])

        # 读逐张统计（容器写在 <ws>/.rdkquant/calib-stats.json）。
        # **这是全部图片的统计，不受回包 files 的 500 条上限影响** ——
        # 筛选必须看到全量，否则会作用在一个偏斜的子集上。
        self._stats = []
        try:
            self._stats = load_stats(containerops.stats_path(self.workspace))
            # 上一次失败可能把筛选区禁用了，成功时要恢复 —— 否则修好之后
            # 界面还是灰的，用户会以为功能坏了（踩过）
            self.filter_section.setEnabled(True)
        except Exception as exc:
            # 读不出来时筛选不可用，但聚合结果仍然有效，不该整页报废
            self.log.append_line(f"[筛选] 逐张统计不可用（{exc}），筛选已禁用")
            self.filter_section.setEnabled(False)

        self._evaluate()
        names = self._display_names()
        if names:
            wanted = sorted({Path(n).stem + ".png" for n in names})
            self.grid.set_images(self.workspace / THUMB_SUBDIR, names=wanted)
            self._displayed = list(names)
        else:
            self.grid.clear()
            self._displayed = []
        self._refresh_funnel()
        self.envChanged.emit()

    def _evaluate(self) -> None:
        payload = self.payload or {}
        preprocess = self.preprocess_box.isChecked()

        verdict = evaluate_probe(
            payload,
            preprocess_on=preprocess,
            calibration_type=self.calibration_type(),
            expected_hw=expected_hw_from_shape(self._suggested("input_shape")),
            directory=str(self.directory),
        )
        self.verdict = verdict

        count = verdict.summary.count
        if count == 0:
            tone = Tone.DANGER
        elif 20 <= count <= 100:
            tone = Tone.OK
        else:
            tone = Tone.WARN
        self.tile_count.set_value(str(count), tone=tone)
        self.tile_count.set_hint("官方建议 20~100 张")

        resolutions = payload.get("resolutions") or []
        if not resolutions:
            self.tile_res.set_value("—")
        elif len(resolutions) == 1:
            w, h, _n = resolutions[0]
            self.tile_res.set_value(f"{w}×{h}", tone=Tone.OK)
        else:
            self.tile_res.set_value(
                f"{len(resolutions)} 种",
                tone=Tone.WARN if preprocess else Tone.DANGER,
            )

        duplicates = payload.get("duplicates") or []
        self.tile_dup.set_value(
            str(len(duplicates)), tone=Tone.WARN if duplicates else Tone.OK
        )

        exposure = payload.get("exposure")
        if exposure:
            self.tile_exposure.set_value(
                f"{exposure.get('min_mean', '—')}~{exposure.get('max_mean', '—')}"
            )
        else:
            self.tile_exposure.set_value("未测量")

        if payload.get("ok") is False:
            self.backend_label.setText(f"扫描失败：{payload.get('error')}")
        elif payload.get("imaging_backend"):
            self.backend_label.setText(
                f"容器里用 {payload['imaging_backend']} 读图。"
            )
        elif payload:
            self.backend_label.setText(
                "容器里没有可用的图像库，只做了文件计数与字节数校验。"
            )

        self.set_findings(verdict.findings)

    # ------------------------------------------------------------------
    def _on_build(self) -> None:
        backend = self.backend()
        if backend is None:
            self.log.append_line("[提示] " + self.environment_hint())
            return
        if self.payload is None:
            self.log.append_line("[校准] 请先扫描一次。")
            return
        if self.verdict is not None and self.verdict.blocked:
            self.log.append_line("[校准] 结论里有错误，先修好再生成。")
            return

        shape = self._suggested("input_shape")
        if not shape:
            self.log.append_line(
                "[校准] 还缺模型输入尺寸。请先在第 1 步导入模型，让工具读出形状。"
            )
            return

        # 条件改过但没刷新预览时拒绝生成 —— 否则产物会和用户在预览里看到的不是一批，
        # 而他无从察觉。
        expected = self._display_names()
        if self._displayed and set(expected) != set(self._displayed):
            self.log.append_line(
                "[校准] 筛选条件已改但预览没刷新，先点「应用筛选并刷新预览」。"
            )
            return

        checked = tuple(self.grid.checked_names())
        if not checked:
            self.log.append_line(
                "[校准] 没有勾选任何图片。在预览里勾选要用的那些，"
                "或者点「全选」。"
            )
            return
        if self._displayed and not set(checked) <= set(self._displayed):
            self.log.append_line("[校准] 勾选与当前预览不一致，请重新勾选。")
            return

        spec = CalibBuildSpec(
            shape=shape,
            layout="NCHW",
            color="bgr",
            dtype=self.dtype_box.currentData(),
            count=self.count_box.value(),
            # **明确指定勾选的那些**：user 的意图优先于自动采样
            only=checked,
        )
        out = self.workspace / spec.output_dir_name
        source = self._staged or (self.workspace / "cal_images")

        def job(emit_line, emit_progress, cancel):
            def on_progress(done, total):
                emit_progress(
                    int(done * 100 / max(1, total)), f"生成校准数据 {done}/{total}"
                )

            return backend.build_calib(
                source, out, spec,
                on_line=emit_line, on_progress=on_progress, cancel=cancel,
            )

        # **先 start 再改标签**：`start()` 在忙的时候返回 False 且不报错，
        # 原来顺序反了 —— 明明没跑起来，界面却写着"生成中…"。
        if self.start(job, self._on_built, label="生成"):
            self.build_label.setText(f"生成中…（{len(checked)} 张）")

    def _on_built(self, payload: dict) -> None:
        if not payload.get("ok"):
            self.build_label.setText(f"生成失败：{payload.get('error')}")
            # **追加而不是替换**：扫描出来的结论（分辨率、张数、曝光）仍然有效，
            # 一次生成失败不该把它们全冲掉。
            findings = list(self.verdict.findings) if self.verdict else []
            findings.append(Finding(
                LEVEL_ERROR, "CALIB_BUILD_FAILED",
                str(payload.get("error") or "生成校准数据失败"),
            ))
            self.set_findings(findings)
            self.envChanged.emit()
            return

        written = payload.get("written")
        total = payload.get("bytes") or 0
        self.build_label.setText(
            f"已写出 {written} 个样本到 {payload.get('out_dir')}/，"
            f"共 {total / 1024 / 1024:.1f} MB，"
            f"每个 {payload.get('bytes_per_sample')} 字节。"
        )
        self.log.append_line(f"[生成] {written} 个样本 → {payload.get('out_dir')}/")

        findings = list(self.verdict.findings) if self.verdict else []
        findings.append(Finding(
            LEVEL_OK, "CALIB_BUILT",
            f"已生成 {written} 个二进制校准样本（{payload.get('dtype')}）",
            "字节数已逐个核对；第 4 步把它写进 config.yaml 的 cal_data_dir。",
        ))
        self.set_findings(findings)
        self.envChanged.emit()

    # ------------------------------------------------------------------
    def apply_to(self, ctx: RunContext) -> None:
        ctx.calibration_type = self.calibration_type()
        ctx.calib = self.verdict.summary if self.verdict is not None else None
