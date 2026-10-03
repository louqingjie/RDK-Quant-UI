# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""S2 模型检查（官方 4.1.1.4）。

跑 `hb_mapper checker`，把输出解析成节点表与分段情况。

两个实测得来的细节：`--march` 的**默认值是 `bayes`**（不是 X5 需要的 `bayes-e`），
所以必须每次显式传；日志落在工作目录的 `hb_mapper_checker.log`（`--help` 里写的
文件名是过时的），所以**优先读那个文件**，stdout 只作回退。

一条诚实性约束：`rc == 0` 但解析不出节点表时**判为未通过**。宁可让用户去看原始
日志，也不能显示一个假的"检查通过"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QWidget

from ...core.findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Finding
from ...core.logparse import parse_checker
from ...core.workflow import RunContext
from ..metrics import METRICS
from ..widgets import Card, MetricTile, TableModel, Tone, make_table
from ..widgets.inputs import NoWheelComboBox
from .stage import StagePage

NODE_HEADERS = ["节点", "设备", "子图", "类型", "ON"]
MARCHES = [
    ("bayes-e", "RDK X5（bayes-e）"),
    ("bernoulli2", "RDK X3（bernoulli2）"),
    ("bayes", "RDK J5（bayes）"),
]

LOG_NAME = "hb_mapper_checker.log"


class CheckPage(StagePage):
    """模型检查页。"""

    def build(self) -> None:
        self.report = None
        self.passed = False
        self.checked_model: Path | None = None
        self.log_path: Path | None = None

        card = Card("运行模型检查", official_ref=self.spec.official_ref)
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(METRICS.space_2)
        self.march_box = NoWheelComboBox()
        for value, label in MARCHES:
            self.march_box.addItem(label, value)
        self.march_box.setToolTip(
            "必须显式选择：hb_mapper 的 --march 默认是 bayes，"
            "不是 X5 需要的 bayes-e。"
        )
        self.run_btn = self._button("运行模型检查", self._on_run, primary=True)
        line.addWidget(self.march_box, 1)
        line.addWidget(self.run_btn)
        line.addStretch(1)
        card.add(row)
        card.add(self._hint_label(
            "会顺带做一次编译，给出 FPS / 延迟估算。"
            "回落到 CPU 的算子不影响能否量化，真实耗时看性能评估的逐节点数据。"
        ))
        self.body.addWidget(card)

        overview = Card("检查结果")
        tiles = QWidget()
        tiles_line = QHBoxLayout(tiles)
        tiles_line.setContentsMargins(0, 0, 0, 0)
        tiles_line.setSpacing(METRICS.space_3)
        self.tile_verdict = MetricTile("结论")
        self.tile_nodes = MetricTile("节点数")
        self.tile_subgraph = MetricTile("子图段数")
        self.tile_cpu = MetricTile("CPU 算子")
        for tile in (self.tile_verdict, self.tile_nodes,
                     self.tile_subgraph, self.tile_cpu):
            tiles_line.addWidget(tile)
        overview.add(tiles)
        self.raw_label = self._hint_label("尚未运行检查。")
        overview.add(self.raw_label)
        self.body.addWidget(overview)

        nodes_card = Card("节点分布")
        self.nodes_model, self.nodes_view = self.make_table_with(NODE_HEADERS)
        nodes_card.add(self.nodes_view)
        self.body.addWidget(nodes_card)

        self.add_findings_card()
        self.refresh_buttons()

    # ------------------------------------------------------------------
    def refresh_buttons(self) -> None:
        ready = self.backend() is not None
        self.run_btn.setEnabled(ready)
        if not ready and self.report is None:
            self.raw_label.setText(self.environment_hint())

    def on_environment_changed(self) -> None:
        self.refresh_buttons()
        self.match_backend_march()

    def match_backend_march(self) -> None:
        """把下拉切到所选镜像对应的 march。"""
        backend = self.backend()
        march = getattr(backend, "march", None) if backend is not None else None
        if not march:
            return
        for index in range(self.march_box.count()):
            if self.march_box.itemData(index) == march:
                self.march_box.setCurrentIndex(index)
                break

    def _on_run(self) -> None:
        backend = self.backend()
        if backend is None:
            self.log.append_line("[提示] " + self.environment_hint())
            return
        model = self.model_path()
        if model is None:
            self.log.append_line("[检查] 还没有导入模型，请先完成第 1 步。")
            return

        march = self.march_box.currentData()
        self.checked_model = model
        self.passed = False
        self.report = None
        # 明确说是对**哪个**模型在检查。model/ 里可能有好几个文件，
        # 不写清楚的话"检查通过"到底针对哪份就是含糊的。
        self.log.append_line(f"[检查] 模型：model/{model.name}，架构：{march}")
        self.envChanged.emit()

        def job(emit_line, _emit_progress, cancel):
            code = backend.check(model, march, on_line=emit_line, cancel=cancel)
            # 优先读工作目录里的日志文件（完整），stdout 只作回退
            log_file = self.workspace / LOG_NAME
            if log_file.is_file():
                try:
                    return code, log_file.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except OSError:
                    pass
            return code, []

        self.start(job, self._on_checked, label="检查")

    def model_path(self) -> Path | None:
        """要检查的模型 —— **必须问 S1，不能自己猜**。

        这里曾经是 `sorted(model_dir.glob("*.onnx"))[0]`，也就是按名字取第一个。
        model/ 会累积历次导入的模型，于是 "0526.onnx" < "0708.onnx" 意味着
        **永远检查最旧的那个**。用户重新导出了 float32 的模型，检查页却还在跑
        旧的 float16 文件，报的还是旧模型的错——极具误导性。

        教训：注释里写着"由 S1 决定"但代码里另外猜一个来源，等于没写。
        S1 已经把它的选择写进 RunContext.suggested_input_parameters / model_path，
        这里只能读它。
        """
        ctx = self.context_provider()
        if ctx is None or ctx.model_path is None:
            return None
        return Path(ctx.model_path)

    def _on_checked(self, result) -> None:
        code, lines = result
        self.log.append_line(f"[检查] hb_mapper checker 退出码 {code}")

        # 归档一份原始日志，方便事后排查
        source = self.workspace / LOG_NAME
        self.log_path = None
        if source.is_file():
            target = self.workspace / "logs" / "checker.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, target)
                self.log_path = target
            except OSError:
                self.log_path = source

        self.report = parse_checker(lines) if lines else None

        if self.report is None:
            self.passed = False
            self.tile_verdict.set_value("未通过", tone=Tone.DANGER)
            for tile in (self.tile_nodes, self.tile_subgraph, self.tile_cpu):
                tile.set_value("—")
            if code != 0:
                # 工具自己失败了。别说成"格式变了"——那是两回事，
                # 会把用户引向错误的方向（去找解析器的问题，而不是修模型）。
                self.raw_label.setText(
                    f"hb_mapper checker 失败（退出码 {code}）。"
                    "日志里的 ERROR 就是原因。"
                )
                self.set_findings([Finding(
                    LEVEL_ERROR, "CHECK_FAILED",
                    f"模型检查失败（退出码 {code}）",
                    "看日志面板里最后一条 ERROR——通常是模型本身不满足官方约束。",
                )])
            else:
                self.raw_label.setText(
                    f"退出码 {code}，但没能从日志里解析出节点表——"
                    "请把日志面板里的原始输出发出来。"
                )
                self.set_findings([Finding(
                    LEVEL_ERROR, "CHECK_UNPARSED",
                    "工具执行成功，但没能解析出节点表",
                    "输出格式可能与解析器不一致，请看日志面板的原始输出。",
                )])
        else:
            # rc==0 且解析出了节点表才算通过。两个条件缺一不可。
            self.passed = code == 0 and self.report.saw_node_table
            self._apply_report(code)
        self.envChanged.emit()

    def _apply_report(self, code: int) -> None:
        report = self.report
        self.tile_verdict.set_value(
            "通过" if self.passed else "未通过",
            tone=Tone.OK if self.passed else Tone.DANGER,
        )
        self.tile_verdict.set_hint(f"退出码 {code}")
        self.tile_nodes.set_value(str(len(report.nodes)))
        self.tile_subgraph.set_value(
            str(report.subgraph_count),
            tone=Tone.WARN if report.subgraph_count > 1 else None,
        )
        self.tile_cpu.set_value(
            str(report.cpu_count),
            tone=Tone.WARN if report.cpu_count else Tone.OK,
        )
        self.raw_label.setText(
            f"共 {len(report.nodes)} 个节点，其中 BPU {report.bpu_count} 个、"
            f"CPU {report.cpu_count} 个，分成 {report.subgraph_count} 段。"
        )

        self.nodes_model.set_rows([
            [node.name, node.device, node.subgraph, node.type, node.on]
            for node in report.nodes
        ])
        self._table(self.nodes_view, self.nodes_model, min_rows=4)

        findings: list[Finding] = []
        if self.passed:
            findings.append(Finding(LEVEL_OK, "CHECK_PASSED", "模型检查通过"))
        else:
            findings.append(Finding(
                LEVEL_ERROR, "CHECK_FAILED", f"模型检查未通过（退出码 {code}）",
                "看日志面板里最后一个 ERROR。",
            ))
        for index, text in enumerate(report.warnings):
            findings.append(Finding(LEVEL_WARN, f"CHECK_WARNING_{index}", str(text)))
        self.set_findings(findings)

    # ------------------------------------------------------------------
    def apply_to(self, ctx: RunContext) -> None:
        # 换了模型，上一次的检查结论立即失效。这一条是**必须**的：
        # 否则会出现"模型换了却仍显示检查通过"的假阳性——门禁会放行一个
        # 从未被检查过的模型，是最危险的一类错误。
        if self.checked_model != ctx.model_path:
            ctx.checker_passed = False
            ctx.checker_log = None
            return
        ctx.checker_passed = bool(self.passed)
        ctx.checker_log = self.log_path
