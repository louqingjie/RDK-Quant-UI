# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""S0 环境准备：宿主机体检 + 官方工具链镜像获取。

对应官方 workflow 的第一步「环境部署」，官方注明只需在第一次使用工具链时做一遍。

这里承载本项目特有的一个坑：RDK 的工具链镜像**不是 docker pull 来的**，
官方只提供离线 tar.gz，正确动作是「下载 → docker load → 解析真实镜像名」。
所以这一页必须能看见进度、能取消、断了能续传。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core import images as img
from ...core.probe import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, GB, HostReport, probe_host
from ...core.workflow import RunContext
from ..metrics import METRICS, font_with_delta
from ..palette import semantic_colors
from ..widgets import (
    Card,
    LogPanel,
    LogView,
    MetricTile,
    TableModel,
    Tone,
    make_table,
)
from ..workers import TaskRunner

IMAGE_HEADERS = ["ID", "板型", "架构", "变体", "离线包", "导入后预计占用", "本机", "说明"]

_LEVEL_TEXT = {LEVEL_OK: "通过", LEVEL_WARN: "警告", LEVEL_ERROR: "错误"}
_LEVEL_TONE = {LEVEL_OK: Tone.OK, LEVEL_WARN: Tone.WARN, LEVEL_ERROR: Tone.DANGER}


class EnvPage(QWidget):
    """环境准备页。"""

    #: 体检结论或镜像状态发生变化，Shell 据此重新评估流程门禁
    envChanged = Signal()

    def __init__(
        self,
        workspace: Path,
        *,
        log_panel: LogPanel,
        with_remote: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.workspace = Path(workspace)
        self.log_panel = log_panel
        self.cache_dir = img.default_cache_dir()
        self.tasks = TaskRunner(self)
        self.specs = img.load_manifest()
        self.local: list[str] = []
        self.report: HostReport | None = None
        # 选中态必须自己记住：刷新镜像表会 reset model，Qt 会把选中行清掉。
        # 不恢复的话，用户点一下行就"失去选择"，按钮随即报"请先选一行"。
        self._selected_id: str | None = None
        self._updating = False
        self._findings: list = []

        m = METRICS
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 用滚动区而不是 QSplitter：字体放大（系统字号 / QT_SCALE_FACTOR）时
        # 内容需要的逻辑高度会随之变大，塞进固定高度的分栏只会互相挤压、
        # 最后靠 Qt 的"不裁剪子控件"表现为控件重叠。滚动区是唯一在任意
        # 字号下都正确的做法。日志已经挪到外壳的停靠面板，分栏的收益也不大了。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(m.page_margin, m.page_margin, m.page_margin, m.page_margin)
        column.setSpacing(m.gap)
        column.addWidget(self._build_env_card())
        column.addWidget(self._build_image_card())
        column.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        self.refresh_local()
        self.refresh_report()
        if with_remote:
            self._probe_remote_sync()

    # ------------------------------------------------------------------
    # 日志与进度由外壳的共享面板提供，页面内不再各带一份
    # ------------------------------------------------------------------
    @property
    def log(self) -> LogView:
        return self.log_panel.log

    @property
    def progress(self) -> QProgressBar:
        return self.log_panel.progress

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_env_card(self) -> QWidget:
        m = METRICS
        card = Card(
            "① 环境体检",
            official_ref="环境部署（工具链首次使用做一遍）",
        )
        card.set_subtitle(
            "UI 跑在宿主机、量化跑在容器里，所以「为什么跑不起来」的原因九成在环境上。"
        )

        # 结论用大字号摆出来，而不是让人去表里找
        tiles = QHBoxLayout()
        tiles.setSpacing(m.space_3)
        self.tile_docker = MetricTile("Docker", "—")
        self.tile_gpu = MetricTile("GPU", "—")
        self.tile_disk = MetricTile("磁盘剩余", "—")
        self.tile_ram = MetricTile("内存", "—")
        for tile in (self.tile_docker, self.tile_gpu, self.tile_disk, self.tile_ram):
            tiles.addWidget(tile, 1)
        card.add_layout(tiles)

        # 结论放在指标下面、明细上面——先给答案，再给依据
        self.verdict = QLabel("")
        self.verdict.setFont(font_with_delta(self.font(), 0, bold=True))
        card.add(self.verdict)

        self.findings_model = TableModel(["级别", "说明", "下一步"])
        self.findings_view = make_table(["级别", "说明", "下一步"], fit_columns=True)
        self.findings_view.setModel(self.findings_model)
        self.findings_view.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.findings_view.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        # 结论型表格不该出现滚动条：高度按"表头 + 4 行"给足
        self.findings_view.setMinimumHeight(m.control_height + 4 * m.row_height + 2)
        card.add(self.findings_view, 1)

        self.recheck_btn = QPushButton("重新体检")
        self.recheck_btn.clicked.connect(self.refresh_report)
        card.add_action(self.recheck_btn)
        return card

    def _build_image_card(self) -> QWidget:
        m = METRICS
        card = Card(
            "② 工具链镜像",
            official_ref="环境部署 · 官方离线包（非 docker pull）",
        )
        card.set_subtitle(
            "官方只提供离线 tar.gz。流程是：下载 → docker load 导入 → 从输出里解析真实镜像名。"
            "支持断点续传，中断后可继续。"
        )

        self.image_model = TableModel(IMAGE_HEADERS)
        self.image_view = make_table(IMAGE_HEADERS, fit_columns=True)
        self.image_view.setModel(self.image_model)
        self.image_view.selectionModel().selectionChanged.connect(self._on_image_selected)
        # 四个镜像要能一眼看全，否则用户得在滚动条里找
        self.image_view.setMinimumHeight(m.control_height + 4 * m.row_height + 2)
        card.add(self.image_view, 1)

        row = QHBoxLayout()
        row.setSpacing(m.space_2)
        self.fetch_btn = QPushButton("下载并导入所选镜像")
        self.fetch_btn.setObjectName("Primary")
        self.fetch_btn.clicked.connect(self._on_fetch)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.tasks.cancel_all)
        self.remote_btn = QPushButton("查询远程体积")
        self.remote_btn.clicked.connect(self._probe_remote_async)
        row.addWidget(self.fetch_btn)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.remote_btn)
        self.cache_label = QLabel(f"离线包缓存：{self.cache_dir}")
        self.cache_label.setObjectName("Muted")
        row.addWidget(self.cache_label, 1)
        card.add_layout(row)
        return card

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def apply_to(self, ctx: RunContext) -> None:
        """把本页的事实写进流程上下文（Shell 调用）。"""
        ctx.docker_ok = bool(self.report and self.report.docker.daemon_ok)
        ctx.image_ref = img.find_loaded(
            self._selected_spec_or_default().image_ref_hint, self.local
        )

    def _selected_spec_or_default(self) -> img.ImageSpec:
        spec = self.selected_spec()
        return spec if spec is not None else self.specs[0]

    def selected_spec(self) -> img.ImageSpec | None:
        rows = self.image_view.selectionModel().selectedRows()
        if not rows:
            return None
        return self.specs[rows[0].row()]

    def refresh_local(self) -> None:
        self.local = img.local_images()

    # ------------------------------------------------------------------
    # 体检
    # ------------------------------------------------------------------
    def refresh_report(self, required: img.ImageSpec | None = None) -> None:
        need = required.size_hint_bytes if required else None
        name = required.display_name if required else ""
        self.report = probe_host(
            self.workspace, required_image_bytes=need, required_image_name=name
        )
        self._findings = list(self.report.findings)
        self._apply_findings()
        self._apply_tiles()

        ok = self.report.can_quantize
        self.verdict.setText(
            "环境就绪，可以开始量化" if ok else "环境未就绪，请按上面的提示处理"
        )
        self.verdict.setStyleSheet(
            f"color: {semantic_colors(self.palette())['success' if ok else 'danger'].name()};"
        )

        report = self.report
        if report.ram.total_bytes:
            self.log.append_line(
                f"[体检] 内存 {report.ram.total_bytes / GB:.1f} GB，"
                f"建议 --shm-size={report.recommended_shm_bytes / GB:.1f}g"
                f"（官方示例的 15g 在 16 GB 机器上会拖死系统）"
            )
        for finding in report.findings:
            if finding.level == LEVEL_ERROR:
                self.log.append_line(f"[体检] {finding.message}")

        self.refresh_images()
        self.envChanged.emit()

    def _apply_findings(self) -> None:
        colors = semantic_colors(self.palette())
        tone_map = {
            LEVEL_OK: colors["success"],
            LEVEL_WARN: colors["warning"],
            LEVEL_ERROR: colors["danger"],
        }
        rows, cell_colors = [], {}
        for i, finding in enumerate(self._findings):
            rows.append(
                [
                    _LEVEL_TEXT.get(finding.level, finding.level),
                    finding.message,
                    finding.hint,
                ]
            )
            cell_colors[(i, 0)] = tone_map.get(finding.level, colors["muted"])
        self.findings_model.set_rows(rows, cell_colors)

    def _apply_tiles(self) -> None:
        report = self.report
        if report is None:
            return
        docker = report.docker
        if docker.cli_path is None:
            self.tile_docker.set_value("未安装", tone=Tone.DANGER)
        elif docker.daemon_ok:
            self.tile_docker.set_value("就绪", tone=Tone.OK)
            self.tile_docker.set_hint(f"客户端 {docker.cli_version} / 服务端 {docker.daemon_version}")
        else:
            self.tile_docker.set_value("未启动", tone=Tone.DANGER)
            self.tile_docker.set_hint("命令存在但守护进程无响应")

        if report.gpu.available:
            self.tile_gpu.set_value(str(len(report.gpu.devices)), "块", Tone.OK)
            self.tile_gpu.set_hint("；".join(report.gpu.devices))
        else:
            self.tile_gpu.set_value("无", tone=Tone.NEUTRAL)
            self.tile_gpu.set_hint("使用 CPU 版镜像")

        if report.disk.total_bytes:
            self.tile_disk.set_value(f"{report.disk.free_bytes / GB:.1f}", "GB")
        if report.ram.total_bytes:
            self.tile_ram.set_value(f"{report.ram.total_bytes / GB:.1f}", "GB")

    # ------------------------------------------------------------------
    # 镜像表
    # ------------------------------------------------------------------
    def _image_rows(self, remote_sizes: dict[str, int] | None = None):
        colors = semantic_colors(self.palette())
        rows, cell_colors = [], {}
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
                cell_colors[(i, 6)] = colors["success"]
        return rows, cell_colors

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

    def _on_image_selected(self, *_args) -> None:
        if self._updating:
            return
        spec = self.selected_spec()
        if spec:
            self._selected_id = spec.id
            self.refresh_report(required=spec)
            self.log.append_line(
                f"[选择] {spec.display_name} → march={spec.march}"
            )

    # ------------------------------------------------------------------
    # 远程体积
    # ------------------------------------------------------------------
    def _probe_remote_sync(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for spec in self.specs:
            info = img.probe_remote(spec.url)
            if info.ok and info.content_length:
                sizes[spec.id] = info.content_length
                self.log.append_line(
                    f"[远程] {spec.id:<15} {img.human_bytes(info.content_length):>10}"
                    f"  支持续传={'是' if info.accept_ranges else '否'}"
                )
            else:
                self.log.append_line(f"[远程] {spec.id:<15} 探测失败：{info.error}")
        self.refresh_images(sizes)
        return sizes

    def _probe_remote_async(self) -> None:
        if self.tasks.busy:
            return
        self.log.append_line("[远程] 正在查询各镜像的精确体积（HEAD 请求）…")

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
        task.line.connect(self.log.append_line)
        task.finished.connect(lambda sizes: self.refresh_images(sizes))

    # ------------------------------------------------------------------
    # 下载并导入
    # ------------------------------------------------------------------
    def _on_fetch(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            self.log.append_line("[提示] 请先在镜像列表里选一行。")
            return
        if self.tasks.busy:
            self.log.append_line("[提示] 已有任务在跑，请先等待或取消。")
            return

        self.fetch_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setFormat("准备中…")
        self.log.append_line("")
        self.log.append_line("=" * 70)
        self.log.append_line(f"[开始] 获取镜像 {spec.display_name}")
        self.log.append_line("       三步流程：下载离线包 → docker load 导入 → 解析真实镜像名")
        self.log.append_line(f"       缓存目录 {self.cache_dir}")

        def job(emit_line, emit_progress, cancel):
            return img.ensure_image(
                spec,
                cache_dir=self.cache_dir,
                on_line=emit_line,
                on_progress=emit_progress,
                cancel=cancel,
            )

        task = self.tasks.start(job)
        task.line.connect(self.log.append_line)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_fetch_done)
        task.failed.connect(self._on_fetch_failed)
        task.cancelled.connect(
            lambda: self._on_fetch_failed("已取消（离线包已保留，可续传）")
        )

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        self.progress.setValue(pct)
        self.progress.setFormat(
            f"下载中 {pct}%  {img.human_bytes(done)} / {img.human_bytes(total)}"
        )

    def _on_fetch_done(self, result) -> None:
        self.fetch_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setValue(100)
        self.progress.setFormat(f"完成：{result.action}")
        self.log.append_line(f"[完成] {result.action} → {result.image_ref}")
        self.refresh_local()
        self.refresh_report()

    def _on_fetch_failed(self, message: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setFormat("失败")
        self.log.append_line(f"[失败] {message}")

    # ------------------------------------------------------------------
    def changeEvent(self, event) -> None:  # noqa: N802
        # 表格单元格的颜色是写死在 model 里的数据，不会随调色板自动变；
        # 主题切换时只需重新上色，**不要**重跑体检（那会去起 docker 进程）。
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_findings()
            self.refresh_images()
            self._apply_tiles()
        super().changeEvent(event)
