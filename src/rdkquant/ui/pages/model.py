# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""S1 浮点模型准备（官方 4.1.1.3）。

能做的事：把 ONNX 拷进工作区、在**容器里**解析图结构、报出问题、并推导
`input_parameters` 供 S4 预填。

一条重要的诚实性约束：`input_layout_train` / `input_type_train` **推不出来**。
训练时的布局和通道顺序只存在于训练脚本里，从 shape 猜是启发式。所以它们一律
以"建议"呈现、要求用户确认，绝不静默采用。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...core.findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Finding
from ...core.workflow import RunContext
from ..metrics import METRICS
from ..widgets import Card, MetricTile, TableModel, make_table
from .stage import StagePage

IO_HEADERS = ["名称", "形状", "类型", "动态维"]
OP_HEADERS = ["算子", "个数"]
PARAM_HEADERS = ["参数", "建议值", "说明"]


class ModelPage(StagePage):
    """浮点模型准备页。"""

    def build(self) -> None:
        self.payload: dict | None = None
        self.model_path: Path | None = None
        self._suggested: dict[str, str] = {}

        # ---- 选择文件 ----
        card = Card("模型文件", official_ref=self.spec.official_ref)
        self.file_label = QLabel("尚未选择模型")
        self.file_label.setWordWrap(True)
        card.add(self.file_label)

        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.space_2)
        self.pick_btn = self._button("选择 ONNX 模型…", self._on_pick, primary=True)
        self.reload_btn = self._button("重新解析", self._on_reload)
        line.addWidget(self.pick_btn)
        line.addWidget(self.reload_btn)
        line.addStretch(1)
        card.add(row)
        card.add(self._hint_label(
            "官方要求模型为静态 shape、输入尺寸固定。模型会被复制到工作区的 "
            "model/ 目录，之后所有步骤都引用这份副本。"
        ))
        self.body.addWidget(card)

        # ---- 概览 ----
        overview = Card("概览")
        tiles = QWidget()
        tiles_line = QHBoxLayout(tiles)
        tiles_line.setContentsMargins(0, 0, 0, 0)
        tiles_line.setSpacing(METRICS.space_3)
        self.tile_io = MetricTile("输入 / 输出")
        self.tile_opset = MetricTile("opset")
        self.tile_ops = MetricTile("算子种类")
        self.tile_ir = MetricTile("IR 版本")
        self.tile_onnx = MetricTile("容器 onnx 版本")
        for tile in (self.tile_io, self.tile_opset, self.tile_ops,
                     self.tile_ir, self.tile_onnx):
            tiles_line.addWidget(tile)
        overview.add(tiles)
        self.body.addWidget(overview)

        # ---- 输入输出 ----
        io_card = Card("输入 / 输出")
        self.io_model = TableModel(IO_HEADERS)
        self.io_view = make_table(self.io_model)
        io_card.add(self.io_view)
        self.body.addWidget(io_card)

        # ---- 算子分布 ----
        ops_card = Card("算子分布", "按出现次数排序，前 15 项")
        self.ops_model = TableModel(OP_HEADERS)
        self.ops_view = make_table(self.ops_model)
        ops_card.add(self.ops_view)
        self.body.addWidget(ops_card)

        # ---- 推导参数 ----
        params_card = Card("推导出的 input_parameters", "供第 4 步预填")
        params_card.add(self._hint_label(
            "⚠️ 形状可以读出来，但**布局与通道顺序读不出来**——它们只存在于训练"
            "脚本里。下表只是按形状给出的建议，第 4 步必须由你确认。"
        ))
        self.params_model = TableModel(PARAM_HEADERS)
        self.params_view = make_table(self.params_model)
        params_card.add(self.params_view)
        self.body.addWidget(params_card)

        self.add_findings_card("模型检查结果")
        self.refresh_buttons()

    # ------------------------------------------------------------------
    def refresh_buttons(self) -> None:
        ready = self.backend() is not None
        self.pick_btn.setEnabled(ready)
        self.reload_btn.setEnabled(ready and self.model_path is not None)
        if self.model_path is not None:
            self.file_label.setText(str(self.model_path))
        else:
            self.file_label.setText(
                "尚未选择模型" if ready else self.environment_hint()
            )

    def on_environment_changed(self) -> None:
        self.refresh_buttons()

    # ------------------------------------------------------------------
    def _on_pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ONNX 模型", str(Path.home()), "ONNX 模型 (*.onnx);;所有文件 (*)"
        )
        if not path:
            return
        source = Path(path)
        target = self.workspace / "model" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        self.log.append_line(f"[模型] 已导入 {source.name} → model/{target.name}")
        self.model_path = target
        self._inspect()

    def _on_reload(self) -> None:
        if self.model_path:
            self._inspect()

    def _inspect(self) -> None:
        backend = self.backend()
        if backend is None or self.model_path is None:
            return
        self.refresh_buttons()
        target = self.model_path

        def job(emit_line, _emit_progress, cancel):
            return backend.inspect_model(target, on_line=emit_line, cancel=cancel)

        self.start(job, self._on_inspected, label="模型")

    def _on_inspected(self, payload: dict) -> None:
        self.payload = payload
        self.refresh_buttons()

        if not payload.get("ok"):
            self.log.append_line(f"[模型] 解析失败：{payload.get('error')}")
            self._suggested = {}
            self.set_findings([Finding(
                LEVEL_ERROR, "MODEL_UNREADABLE",
                str(payload.get("error") or "模型解析失败"),
                "确认它是有效的 ONNX 文件；也可能是容器里缺 onnx。",
            )])
            self.envChanged.emit()
            return

        inputs = payload.get("inputs") or []
        outputs = payload.get("outputs") or []
        dynamic = [i for i in inputs if i.get("dynamic")]

        self.tile_io.set_value(f"{len(inputs)} / {len(outputs)}")
        self.tile_opset.set_value(str(payload.get("opset") or "—"))
        histogram = payload.get("op_histogram") or []
        total_ops = sum(count for _name, count in histogram)
        self.tile_ops.set_value(str(len(histogram)), "种")
        self.tile_ops.set_hint(f"共 {total_ops} 个节点")
        self.tile_ir.set_value(str(payload.get("ir_version") or "—"))
        self.tile_onnx.set_value(str(payload.get("onnx") or "—"))

        rows = []
        for item in [*inputs, *outputs]:
            shape = item.get("shape") or []
            rows.append([
                item.get("name", ""),
                "×".join("?" if d in (None, 0) else str(d) for d in shape),
                item.get("dtype", ""),
                "是" if item.get("dynamic") else "否",
            ])
        self.io_model.set_rows(rows)
        self._table(self.io_view, self.io_model, min_rows=2)

        self.ops_model.set_rows([[name, str(count)] for name, count in histogram[:15]])
        self._table(self.ops_view, self.ops_model, min_rows=3)

        self._apply_suggestions(inputs)
        self._apply_findings(payload, dynamic)
        self.envChanged.emit()

    def _apply_suggestions(self, inputs: list[dict]) -> None:
        names = [i.get("name", "") for i in inputs]
        shape = (inputs[0].get("shape") if inputs else None) or []
        shape_text = "×".join(str(d) for d in shape) if shape else ""
        # 形态启发式：末维是 1/3/4 且第二维不是，更像 NHWC；否则按 NCHW。**只是建议。**
        layout = "NCHW"
        if shape and len(shape) == 4 and shape[-1] in (1, 3, 4) \
                and shape[1] not in (1, 3, 4):
            layout = "NHWC"
        rows = [
            ["input_name", ";".join(names), "多输入用分号分隔（官方写法）"],
            ["input_shape", shape_text, "从 ONNX 图形状读出，可信"],
            ["input_layout_train", layout, "⚠️ 猜的，请按训练时的真实布局确认"],
            ["input_type_train", "rgb", "⚠️ 猜的，按训练时的通道顺序确认"],
            ["norm_type", "no_preprocess", "训练时若做过归一化，第 4 步要改"],
        ]
        self.params_model.set_rows(rows)
        self._table(self.params_view, self.params_model, min_rows=4)
        # 存一份给 apply_to 用——不去反查表格模型（那是 UI 的内部结构）
        self._suggested = {row[0]: row[1] for row in rows if row[0] and row[1]}

    def _apply_findings(self, payload: dict, dynamic: list[dict]) -> None:
        findings: list[Finding] = []
        for index, text in enumerate(payload.get("problems") or []):
            findings.append(Finding(LEVEL_ERROR, f"MODEL_PROBLEM_{index}", str(text)))
        for index, text in enumerate(payload.get("warnings") or []):
            findings.append(Finding(LEVEL_WARN, f"MODEL_WARNING_{index}", str(text)))
        if not dynamic and not payload.get("problems"):
            findings.append(Finding(
                LEVEL_OK, "MODEL_STATIC", "模型是静态 shape，符合官方要求",
            ))
        if dynamic:
            findings.append(Finding(
                LEVEL_WARN, "MODEL_DYNAMIC",
                f"有 {len(dynamic)} 个输入含动态维度",
                "hb_mapper 能检查它，但 infer 不支持动态 shape；"
                "建议回到训练侧固定输入尺寸后重新导出。",
            ))
        self.set_findings(findings)

    # ------------------------------------------------------------------
    def apply_to(self, ctx: RunContext) -> None:
        ctx.model_path = self.model_path
        if self.payload and self.payload.get("ok"):
            inputs = self.payload.get("inputs") or []
            ctx.model_static = (
                all(not i.get("dynamic") for i in inputs) if inputs else None
            )
            ctx.model_problems = list(self.payload.get("problems") or [])
            ctx.suggested_input_parameters = dict(self._suggested)
        elif self.payload is not None:
            ctx.model_static = None
            ctx.model_problems = [str(self.payload.get("error") or "模型解析失败")]
            ctx.suggested_input_parameters = {}
        else:
            ctx.model_static = None
            ctx.model_problems = []
