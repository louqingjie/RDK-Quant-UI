# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""模型量化与编译页（S4）。

这一页做的事：**写 `config.yaml` → 跑 `hb_mapper makertbin` → 认产物 →
三方面校验**。

策略判断全在 `core/conversion.py` 里，这里只负责接线与呈现 —— 和
`check.py` / `calib.py` 同一条边界。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QWidget

from ...core import calibpick
from ...core.backends import Workspace
from ...core.conversion import (
    MAKERTBIN_LOG_NAME,
    ConversionSpec,
    clear_output,
    discover_artifacts,
    evaluate_conversion,
    missing_artifacts,
    parse_makertbin,
    read_log,
    summarize_artifacts,
    write_config,
)
from ...core.findings import LEVEL_WARN, Finding
from ...core.workflow import Stage
from ..metrics import METRICS
from ..widgets import Card, MetricTile
from .stage import StagePage

ARTIFACT_HEADERS = ["产物", "文件", "大小"]
SUMMARY_HEADERS = ["项", "值"]


def format_size(size: int) -> str:
    if size <= 0:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class ConvertPage(StagePage):
    """模型量化与编译页。"""

    @property
    def _ws(self) -> Workspace:
        """带 `output_dir` / `config_path` / `rel()` 的工作区。

        ⚠️ `StagePage.workspace` 是**裸 `Path`**，只有 `Workspace` 才有那些成员。
        这个项目**已经踩过一次同样的坑**（`Path` 当 `Workspace` 传进
        `make_backend`，只在真机上才炸，见 `test_environment_ready_builds_the_backend`
        的注释）—— 我写这一页时又踩了一次，被端到端测试当场抓住。
        所以这里给一个显式属性，而不是到处 `Workspace(self.workspace)`。
        """
        return Workspace(self.workspace)

    def build(self) -> None:
        self.verdict = None
        self.artifacts: dict[str, Path] = {}

        # ---- 卡 1：运行 ----
        card = Card("运行量化编译", official_ref=self.spec.official_ref)
        self.summary_model, self.summary_view = self.make_table_with(SUMMARY_HEADERS)
        card.add(self.summary_view)

        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.space_2)
        self.run_btn = self._button("开始转换", self._on_run, primary=True)
        line.addWidget(self.run_btn)

        # 官方要求先过模型检查；用户确实要跳过时必须**显式勾选**，
        # 而不是我们替他决定（门禁在 core/workflow.py 里认这个字段）
        self.override_box = QCheckBox("跳过模型检查")
        self.override_box.setToolTip(
            "官方要求转换前先通过模型检查。勾选表示你确认要跳过它。"
        )
        self.override_box.toggled.connect(lambda _on: self.envChanged.emit())
        line.addWidget(self.override_box)
        line.addStretch(1)
        card.add(row)

        self.estimate_label = self._hint_label("")
        card.add(self.estimate_label)
        self.body.addWidget(card)

        # ---- 卡 2：产物 ----
        artifacts_card = Card("转换产物")
        self.artifact_model, self.artifact_view = self.make_table_with(ARTIFACT_HEADERS)
        artifacts_card.add(self.artifact_view)
        self.artifact_hint = self._hint_label("尚未转换。")
        artifacts_card.add(self.artifact_hint)
        self.body.addWidget(artifacts_card)

        # ---- 卡 3：结论 ----
        self.add_findings_card()
        self._apply_artifacts()
        self.refresh_buttons()

    # ------------------------------------------------------------------
    def refresh_buttons(self) -> None:
        ready = self.backend() is not None
        self.run_btn.setEnabled(ready)
        self._update_summary()
        self._update_estimate()

    def on_environment_changed(self) -> None:
        self.refresh_buttons()

    def _march(self) -> str:
        backend = self.backend()
        march = getattr(backend, "march", None) if backend is not None else None
        return march or "bayes-e"

    # ------------------------------------------------------------------
    def _update_summary(self) -> None:
        """把「这次要写进 config.yaml 的是什么」摊开给用户看。

        不上这一屏的话，用户点一次转换能拿到的只有一屏日志 —— 出问题时
        不知道该看哪个参数。
        """
        ctx = self.context_provider()
        spec = self.conversion_spec(ctx, report=False)
        if spec is None:
            self.summary_model.set_rows([["状态", "还不能转换，看下面的结论"]])
            self._table(self.summary_view, self.summary_model, min_rows=1)
            return

        calibration = spec.calibration_type
        if not spec.needs_calibration_data:
            calibration += "（不需要校准数据）"
        rows = [
            ["模型", spec.onnx_model],
            ["产物前缀", spec.output_prefix],
            ["架构 march", spec.march],
            ["输入", f"{spec.input_name} {spec.input_shape} "
                     f"{spec.input_type_train}/{spec.input_layout_train}"],
            ["板端输入", spec.input_type_rt],
            ["校准方式", calibration],
            ["校准数据", spec.cal_data_dir or "（无）"],
        ]
        self.summary_model.set_rows(rows)
        self._table(self.summary_view, self.summary_model, min_rows=4)

    def _update_estimate(self) -> None:
        """校准的**事前**提示。

        ⚠️ **这里刻意不给预估时间。** 曾经给过（「每张约 164 秒」），但那是
        **在一个数据集上量出来的**：同一台机器、同一个模型，换一组校准图，
        单张成本从 168 秒掉到 **2.7 秒** —— 差 60 倍。

        原因官方 FAQ 写得很清楚（6.6.1.4 / 6.6.1.5）：`default` 与 `mix` 都是
        **搜索**策略，搜索深度由**数据**决定。任何跑前的常数都是在编。

        所以这里只说**能确定的事**：多少张、官方建议多少、超了会怎样。
        真实速度由 `hb_mapper` 自己的进度条在跑的时候报出来 —— 那是实测值。
        """
        ctx = self.context_provider()
        if ctx is None or not ctx.calibration_needs_data:
            self.estimate_label.setText("")
            return
        count = ctx.calib.count if ctx.calib else 0
        if not count:
            self.estimate_label.setText("")
            return

        text = (f"本次校准 {count} 张。官方建议 20~100 张；超过 "
                f"{calibpick.SLOW_THRESHOLD} 张后会明显变慢"
                f"（{ctx.calibration_type} 是搜索策略，成本随张数线性增长）。")
        if count > calibpick.SLOW_THRESHOLD:
            text += "建议回第 3 步点「自动挑选」降下来。"
        self.estimate_label.setText(text)

    # ------------------------------------------------------------------
    def conversion_spec(self, ctx, *, report: bool = True) -> ConversionSpec | None:
        """从 `RunContext` 拼出 `config.yaml` 的参数。

        缺东西时**返回 None**，并把原因写进日志（`report=True` 时）——
        和 `CheckPage.model_path()` 一样，不替用户猜。
        """
        def complain(message: str) -> None:
            if report:
                self.log.append_line(f"[转换] {message}")

        if ctx is None:
            complain("拿不到流程上下文，无法生成配置。")
            return None
        if not ctx.model_path:
            complain("还没有导入模型，请先完成第 1 步。")
            return None
        if ctx.calibration_needs_data and not (ctx.calib and ctx.calib.usable):
            complain("校准数据未就绪，请先完成第 3 步。")
            return None

        # `input_type_train` / `input_layout_train` **推不出来**（S1 的注释写明了），
        # 这里用的是 S1 让用户确认过的那份建议值
        params = dict(ctx.suggested_input_parameters or {})
        if not params.get("input_shape"):
            complain("模型的输入形状未知，请回第 1 步重新解析模型。")
            return None

        calib = ctx.calib
        return ConversionSpec(
            # `rel_dot()` 而不是 `rel()`：config.yaml 里官方一律写相对路径，
            # 而且真跑验证过的就是 `./model/x.onnx` 这种形式
            onnx_model=self._ws.rel_dot(ctx.model_path),
            output_prefix=Path(ctx.model_path).stem,
            march=self._march(),
            input_name=params.get("input_name") or "images",
            input_shape=params.get("input_shape") or "",
            input_type_train=params.get("input_type_train") or "rgb",
            input_layout_train=params.get("input_layout_train") or "NCHW",
            calibration_type=ctx.calibration_type,
            cal_data_dir=(calib.cal_data_dir if calib else ""),
            cal_data_type=(calib.cal_data_type if calib else "float32"),
        )

    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        backend = self.backend()
        if backend is None:
            self.log.append_line("[提示] " + self.environment_hint())
            return

        spec = self.conversion_spec(self.context_provider())
        if spec is None:
            return

        # 转换前**清空 output/**：makertbin 失败时也会往里写东西（实测
        # shape_inference_fail.onnx），留着会让上一次的残留把
        # 「5 个产出物齐全」喂成假通过
        removed = clear_output(self._ws.output_dir)
        if removed:
            self.log.append_line(f"[转换] 已清空 output/（{removed} 个文件）")

        write_config(self._ws.config_path, spec)
        self.log.append_line(
            f"[转换] 配置已写入 config.yaml：{spec.onnx_model} → {spec.output_prefix}"
        )
        self.verdict = None
        self.artifacts = {}
        self._apply_artifacts()

        def job(emit_line, _emit_progress, cancel):
            code = backend.compile(on_line=emit_line, cancel=cancel)
            # **解析读工作区日志，不读 stdout** —— stdout 在 120 列会硬折行，
            # 把节点表切成两行一条（实测：601 行 vs 352 行）
            lines = read_log(self.workspace / MAKERTBIN_LOG_NAME)
            report = parse_makertbin(lines) if lines else None
            artifacts = discover_artifacts(self._ws.output_dir)
            return evaluate_conversion(report, artifacts, spec=spec, returncode=code)

        self.start(job, self._on_converted, label="转换")

    def _on_converted(self, verdict) -> None:
        self.verdict = verdict
        self.artifacts = dict(verdict.artifacts)
        self._apply_artifacts()
        self.set_findings(verdict.findings)
        missing = missing_artifacts(self.artifacts)
        if verdict.ok:
            self.log.append_line("[转换] 完成，5 个产出物齐全。")
        elif missing:
            self.log.append_line(f"[转换] 结束，但缺：{'、'.join(missing)}")
        self.envChanged.emit()          # 门禁要重新评估，S5 才会解锁

    def _apply_artifacts(self) -> None:
        rows = [
            [label, name, format_size(size)]
            for label, name, size in summarize_artifacts(self.artifacts)
        ]
        self.artifact_model.set_rows(rows)
        self._table(self.artifact_view, self.artifact_model, min_rows=5)

        if self.verdict is None:
            self.artifact_hint.setText("尚未转换。")
        elif self.verdict.ok:
            self.artifact_hint.setText(
                f"产物在 {self.workspace.name}/output/，可直接用于性能与精度评估。"
            )
        else:
            failed = [name for name, ok in self.verdict.checks.items() if not ok]
            self.artifact_hint.setText(f"未通过：{'、'.join(failed)}。详见结论。")

    # ------------------------------------------------------------------
    def apply_to(self, ctx) -> None:
        if ctx is None:
            return
        ctx.conversion_override = self.override_box.isChecked()
        if self.verdict is not None:
            ctx.artifacts = dict(self.verdict.artifacts)
            ctx.conversion_checks = dict(self.verdict.checks)
