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
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.calib import (
    CalibBuildSpec,
    evaluate_probe,
    expected_hw_from_shape,
)
from ...core.findings import LEVEL_ERROR, LEVEL_OK, Finding
from ...core.workflow import RunContext
from ..metrics import METRICS
from ..widgets import Card, MetricTile, Tone
from ..widgets.thumbnailgrid import ThumbnailGrid
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

        # ---- 缩略图 ----
        thumbs_card = Card("预览", "缩略图由容器生成")
        self.grid = ThumbnailGrid()
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
        self.count_box.setRange(0, 1000)
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
    def refresh_buttons(self) -> None:
        ready = self.backend() is not None
        self.pick_btn.setEnabled(ready)
        self.scan_btn.setEnabled(ready)
        self.build_btn.setEnabled(ready)
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
                staged, shape=shape, dtype=dtype,
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
        self._evaluate()
        self.grid.set_images(self.workspace / THUMB_SUBDIR)
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

        spec = CalibBuildSpec(
            shape=shape,
            layout="NCHW",
            color="bgr",
            dtype=self.dtype_box.currentData(),
            count=self.count_box.value(),
        )
        out = self.workspace / spec.output_dir_name
        source = self._staged or (self.workspace / "cal_images")
        self.build_label.setText("生成中…")

        def job(emit_line, emit_progress, cancel):
            def on_progress(done, total):
                emit_progress(
                    int(done * 100 / max(1, total)), f"生成校准数据 {done}/{total}"
                )

            return backend.build_calib(
                source, out, spec,
                on_line=emit_line, on_progress=on_progress, cancel=cancel,
            )

        self.start(job, self._on_built, label="生成")

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
