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

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QWidget

from ...core.findings import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Finding
from ...core.logparse import parse_checker
from ...core.workflow import RunContext
from ..metrics import METRICS
from ..widgets import Card, MetricTile, TableModel, Tone, make_table
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
        self.march_box = QComboBox()
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
            "检查阶段会顺带做一次编译并给出 FPS / 延迟估算。官方口径：这里不必"
            "纠结回落 CPU 的算子，真实开销要看性能评估里的逐节点耗时。"
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
        """模型由 S1 决定；从工作区里找那份副本。

        不直接问 S1 页面——页面之间不耦合，工作区里的 model/ 才是共同的事实来源。
        """
        model_dir = self.workspace / "model"
        candidates = sorted(model_dir.glob("*.onnx")) if model_dir.is_dir() else []
        return candidates[0] if candidates else None

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
            self.raw_label.setText(
                f"退出码 {code}。没能从日志里解析出节点表——"
                "格式可能变了，请看日志面板里的原始输出。"
            )
            self.set_findings([Finding(
                LEVEL_ERROR, "CHECK_UNPARSED",
                "没能解析出节点表，本次检查判为未通过",
                "请看日志面板的原始输出；如果工具确实通过了，说明输出格式与"
                "解析器不一致（解析器可以容忍未知列，但不该完全认不出来）。",
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
