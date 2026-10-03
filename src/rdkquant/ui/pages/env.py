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
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...core import images as img
from ...core.probe import (
    GB,
    LEVEL_ERROR,
    LEVEL_OK,
    LEVEL_WARN,
    HostReport,
    disk_verdict,
    probe_host,
)
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
    fit_columns_to_content,
    make_table,
)
from ..workers import JobCoordinator, TaskRunner

_LEVEL_TEXT = {LEVEL_OK: "通过", LEVEL_WARN: "警告", LEVEL_ERROR: "错误"}


def fit_table_height(view: QTableView, model: TableModel, *, min_rows: int = 3) -> None:
    """把结论型表格的高度钉成"表头 + N 行"。

    为什么不能用 `setMinimumHeight`：`QTableView` 的默认 `sizeHint()` 是
    256×192，放进布局它会先按 sizeHint 拿走 192px，而 minimumHeight 只是**下限**、
    拦不住它。结果就是表格里留一大片空白，同时把下面那张卡片顶出可视区
    —— 实测现象是镜像表只剩表头露在外面，四行数据全在折叠线以下。

    行高刻意**问视图要实际值**（`rowHeight()`），而不是拿尺寸令牌去乘：
    令牌是按 `app.font()` 算的，而表格实际渲染用的字体可能与之不同，
    两者对不上时算出来的高度会把最后一行裁掉（带出内部滚动条）。
    结论型表格本来就不该出现滚动条，所以直接定高最省事、也最可预测。
    行数是动态的（体检结论可能 2~6 条），所以每次填完数据都要重算一次。
    """
    rows = max(min_rows, model.rowCount())
    row_height = view.rowHeight(0) if model.rowCount() > 0 else METRICS.row_height
    row_height = max(row_height, METRICS.row_height)
    view.setFixedHeight(view.horizontalHeader().height() + rows * row_height + 2)


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
        jobs=None,
        parent=None,
    ):
        super().__init__(parent)
        self.workspace = Path(workspace)
        self.log_panel = log_panel
        self.cache_dir = img.default_cache_dir()
        self.tasks = TaskRunner(self)
        #: 全应用单飞协调器（见 ui/workers.py: JobCoordinator）。
        #: 没传就自建一个私有的 —— 独立构造本页时退化成"只看这一页"。
        self.jobs = jobs if jobs is not None else JobCoordinator(self)
        # 信号**等界面建完再连** —— `_on_jobs_changed` 要碰 fetch_btn 这些控件。
        self.specs = img.load_manifest()
        self.local: list[str] = []
        self.report: HostReport | None = None
        # 选中态必须自己记住：刷新镜像列表会 clear() 下拉栏，Qt 会把当前项丢掉。
        # 不恢复的话，用户选完再看一眼别的页面回来，"选择"就没了。
        self._selected_id: str | None = None
        #: 查询到的远程体积（覆盖清单里的 size_hint_bytes）。详情行要用它。
        self._remote_sizes: dict[str, int] = {}
        #: 自动探测每个会话只做一次；手动点「查询远程体积」不受这个限制
        self._remote_probed = False
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
            # 截图模式：**同步**探测，这样截出来的图上就是真实体积
            self._probe_remote_sync()
        # 正常启动时由 refresh_report() 触发**异步**自动探测（见 _auto_probe_remote）
        self.jobs.changed.connect(self._on_jobs_changed)

    def _on_jobs_changed(self) -> None:
        """全局单飞：有人（可能是我自己）在跑 → 动作按钮禁用。

        「取消」按钮的处理要分情况，因为它连的是**本页的** runner
        （`self.tasks.cancel_all`）：

        * 我在跑 → 取消可用，它取消的确实是我的任务
        * 别页在跑 → 取消也禁用。它点了不会有任何作用，
          **留着反而让人以为能取消别人的任务**
        """
        if self.jobs.is_owner(self):
            self.fetch_btn.setEnabled(False)
            self.remote_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)
            return
        if self.jobs.busy:
            self.fetch_btn.setEnabled(False)
            self.remote_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            return
        self.fetch_btn.setEnabled(True)
        self.remote_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

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
        self.findings_view = make_table(["级别", "说明", "下一步"])
        self.findings_view.setModel(self.findings_model)
        # 列宽模式必须在 setModel 之后设（列数来自 model）
        self.findings_view.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.findings_view.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        # 高度由 fit_table_height() 在填完数据后按行数钉死，这里只留一个起步值
        card.add(self.findings_view)

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

        # ── 选择：下拉栏 ──
        #
        # 这里原来是一张 8 列表格（ID/板型/架构/变体/离线包/导入后预计占用/本机/说明）。
        # 问题不是信息不够，是**它看着像数据表，不像选择器**：用户在这一步要做的事
        # 只有"选一个镜像"，却得先在表里点一行，而选中态只有一行淡淡的高亮；
        # 首次进入时更是**什么都没选中**，看不出默认会用哪个。
        #
        # 改成下拉栏后：选项文字直接带状态（已导入 / 需下载 1.7 GB），
        # 选中项完整信息跟在下面。表格那 8 列一条信息都没丢，只是换了位置。
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(m.space_1)
        picker_label = QLabel("工具链镜像")
        picker_label.setObjectName("Muted")
        box.addWidget(picker_label)

        self.image_box = QComboBox()
        # 未选时显示这句（currentIndex == -1）。**故意不给默认选中项** ——
        # 见 _resolve_selection()：只自动选中"本机已经导入"的镜像。
        self.image_box.setPlaceholderText("请选择工具链镜像…")
        self.image_box.setToolTip("选一个工具链镜像。括号里是它对应的芯片架构和本机状态。")
        self.image_box.currentIndexChanged.connect(self._on_image_selected)
        box.addWidget(self.image_box)
        card.add(holder)

        # 选中项的完整信息：原来摊在表格里的那几列，现在跟着选择走
        self.image_detail = QLabel("—")
        self.image_detail.setObjectName("Muted")
        self.image_detail.setWordWrap(True)
        card.add(self.image_detail)

        self.image_note = QLabel("")
        self.image_note.setObjectName("Muted")
        self.image_note.setWordWrap(True)
        card.add(self.image_note)

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
        """把本页的事实写进流程上下文（Shell 调用）。

        **没有选中的镜像时就把 `image_ref` 留空**，交给门禁去挡。
        以前这里写的是 `_selected_spec_or_default()`，会静默兜底到 `specs[0]` ——
        等于"界面没选，代码替你选了"，而猜错一个镜像是几百 MB 到 10 GB 的下载。
        """
        ctx.docker_ok = bool(self.report and self.report.docker.daemon_ok)
        spec = self.selected_spec()
        ctx.image_selected = spec.display_name if spec else None
        ctx.image_ref = (
            img.find_loaded(spec.image_ref_hint, self.local) if spec else None
        )

    def selected_spec(self) -> img.ImageSpec | None:
        """当前下拉栏选中的镜像。下拉栏始终有选中项，所以正常不会返回 None。"""
        index = self.image_box.currentIndex()
        if index < 0 or index >= len(self.specs):
            return None
        return self.specs[index]

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
        # 环境就绪后自动问一次真实体积（每次会话只做一次，Docker 没起来就跳过）
        self._auto_probe_remote()
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
        fit_table_height(self.findings_view, self.findings_model, min_rows=3)

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
    # 镜像下拉栏
    # ------------------------------------------------------------------
    def _disk_verdict(self, present: str | None, size: int | None):
        """按当前剩余空间算这个镜像装不装得下；返回 DiskVerdict 或 None。

        **用的就是体检那份实现**（`core/probe.py: disk_verdict`），
        不在这里另写一套阈值 —— 两处各写一份，迟早漂移成
        "结论说不够、下拉栏说够"。
        """
        report = self.report
        if not size or report is None:
            return None
        return disk_verdict(report.disk.free_bytes, size)

    def _disk_suffix(self, present: str | None, size: int | None) -> str:
        """选项文字末尾的磁盘提示。

        目的是让用户**按下去之前**就知道装不装得下，而不是选完再被结论页告知。

        两条刻意的取舍：

        * **已经导入的镜像不加提示** —— 它不用再下载，下载体积与这个决定无关。
        * **不"禁止"选不下的镜像**。磁盘需求是 `size × 2.2` 的**估算**（±15%），
          拿估算值堵死用户的选择会误伤；而且剩余空间是移动靶（他可能先选中、
          清完盘再下）。给信息，不夺选择权。
        """
        if present:
            return ""
        verdict = self._disk_verdict(present, size)
        if verdict is None or verdict.ok or not verdict.short:
            return ""
        return f"　·　⚠️ {verdict.short}"

    def _combo_text(self, spec: img.ImageSpec, present: str | None, size: int | None) -> str:
        """下拉栏里的一行。

        **决定选择的信息必须在选项文字里**（本机有没有、要下多少、装不装得下）——
        藏到下面的详情里就还得点一遍才知道。
        """
        if present:
            base = f"{spec.display_name}　·　已导入"
        elif size:
            base = f"{spec.display_name}　·　需下载 {img.human_bytes(size)}"
        else:
            base = f"{spec.display_name}　·　需下载（体积未知，可先查询）"
        return base + self._disk_suffix(present, size)

    @staticmethod
    def _detail_text(spec: img.ImageSpec, present: str | None, size: int | None) -> str:
        """选中项的完整信息 —— 原来摊在表格里的那几列。"""
        parts = [
            f"架构 {spec.march}",
            f"Python {spec.python}" if spec.python else "",
        ]
        if size:
            # 解压后的镜像层通常是压缩包的 2~2.5 倍，这个数字直接决定磁盘够不够
            parts.append(f"离线包 {img.human_bytes(size)}")
            parts.append(f"导入后约 {img.human_bytes(int(size * 2.2))}")
        else:
            # 体积未知时**只说一次**，别写成"离线包 未知 · 导入后约 未知"
            parts.append("离线包体积未知（可点「查询远程体积」）")
        parts.append(f"本机：{present}" if present else "本机：未导入")
        return "　·　".join(p for p in parts if p)

    def _update_image_detail(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            self.image_detail.setText("—")
            self.image_note.setText("")
            return
        size = self._remote_sizes.get(spec.id) or spec.size_hint_bytes
        present = img.find_loaded(spec.image_ref_hint, self.local)
        self.image_detail.setText(self._detail_text(spec, present, size))
        self.image_note.setText(spec.note)

    def refresh_images(self, remote_sizes: dict[str, int] | None = None) -> None:
        # ⚠️ **只增不减**：`_remote_sizes` 是"已经问到的精确体积"的缓存，
        # 刷新**显示**不该把它丢掉。
        #
        # 这里踩过一个 bug：原来是 `self._remote_sizes = dict(remote_sizes or {})`，
        # 而 `refresh_report()` 每次都无参调用本方法 —— 于是刚探测到的体积，
        # 会被紧接着的一次报告刷新擦成空。表现是"HEAD 明明成功了，界面还是估算值"。
        if remote_sizes:
            self._remote_sizes.update(remote_sizes)
        self._updating = True                 # 重建选项期间不发信号，否则会连环触发体检
        try:
            self.image_box.clear()
            for spec in self.specs:
                size = self._remote_sizes.get(spec.id) or spec.size_hint_bytes
                present = img.find_loaded(spec.image_ref_hint, self.local)
                self.image_box.addItem(self._combo_text(spec, present, size))
                # tooltip 放完整信息：阈值判定 + 官方说明，鼠标停一下就能看全
                verdict = self._disk_verdict(present, size)
                tooltip = self._detail_text(spec, present, size)
                if verdict is not None and not verdict.ok:
                    tooltip += f"\n⚠️ {verdict.message}"
                    if verdict.hint:
                        tooltip += f"\n   {verdict.hint}"
                if spec.note:
                    tooltip += f"\n{spec.note}"
                self.image_box.setItemData(
                    self.image_box.count() - 1,
                    tooltip,
                    Qt.ItemDataRole.ToolTipRole,
                )
            # 恢复上次的选择，或在"没什么可决定"时自动选一个（见下）
            self.image_box.setCurrentIndex(self._resolve_selection())
        finally:
            self._updating = False
        self._update_image_detail()

    def _resolve_selection(self) -> int:
        """决定下拉栏选哪一项；返回 **-1 表示还没选**。

        * 上次选过的（`_selected_id`）优先恢复 —— 用户的选择不该被刷新弄丢
        * 否则**只自动选中"本机已经导入"的**那个：镜像已经在本地了，
          选它不需要下载任何东西，没有可决定的
        * 一个都没导入 → 保持未选，由 `core/workflow.py` 的环境门禁挡住，
          并在步骤条上说明"还没选工具链镜像"

        **不替用户猜。** 之前这里是默认选第 0 项，而猜错一个镜像是
        几百 MB 到 10 GB 的下载 —— 用户会以为下的是另一个。
        """
        if self._selected_id:
            for index, spec in enumerate(self.specs):
                if spec.id == self._selected_id:
                    return index
        for index, spec in enumerate(self.specs):
            if img.find_loaded(spec.image_ref_hint, self.local):
                return index
        return -1

    def _on_image_selected(self, _index: int = -1) -> None:
        if self._updating:
            return
        spec = self.selected_spec()
        if spec:
            self._selected_id = spec.id
            self._update_image_detail()
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

    def _probe_remote_async(self, *, specs=None, silent: bool = False) -> None:
        """异步问远程体积。

        `specs` 默认问全部；自动探测只问本机还没有的那几个。
        `silent` 供自动探测用：不问就不打"正在查询"，失败也只留一行摘要，
        免得离线启动时刷四条失败日志吓人。
        """
        if self.tasks.busy:
            return
        targets = list(specs if specs is not None else self.specs)
        if not targets:
            return
        if not silent:
            self.log.append_line("[远程] 正在查询各镜像的精确体积（HEAD 请求）…")

        def job(emit_line, _emit_progress, cancel):
            sizes: dict[str, int] = {}
            failures: list[str] = []
            for spec in targets:
                if cancel.is_set():
                    break
                info = img.probe_remote(spec.url)
                if info.ok and info.content_length:
                    sizes[spec.id] = info.content_length
                    emit_line(f"[远程] {spec.id:<15} {img.human_bytes(info.content_length)}")
                else:
                    failures.append(spec.id)
                    # 自动探测失败**不逐条刷日志** —— 离线启动会连刷四条，很吓人。
                    # 汇总留给 done() 说一句。
                    if not silent:
                        emit_line(f"[远程] {spec.id:<15} 探测失败：{info.error}")
            return sizes, failures

        def done(result):
            sizes, failures = result
            # refresh_images 是"只增不减"的，直接把新探测到的并进去就行
            self.refresh_images(sizes)
            if not silent or not failures:
                return
            if not sizes:
                # 离线启动：说清"不影响用"就够了，不要留四行错误
                self.log.append_line(
                    "[远程] 连不上官方服务器，体积用清单里的估算值"
                    "（不影响离线使用，点「查询远程体积」可重试）"
                )
            else:
                self.log.append_line(
                    f"[远程] {len(failures)} 个镜像没取到精确体积，那几个用清单估算值"
                )

        task = self.tasks.start(job)
        task.line.connect(self.log.append_line)
        task.finished.connect(done)

    def _auto_probe_remote(self) -> None:
        """环境就绪后自动问一次远程体积。

        为什么值得自动做：清单里的 `size_hint_bytes` 是**某个时刻的快照**，
        实测已经和服务器对不上（见 `tests/fixtures/CONTAINER_FINDINGS.md` 第 9 节），
        而且 X3 两条原先根本没有体积，磁盘检查跑不起来。
        HEAD 请求不带 body，很便宜。

        两条守卫：

        * **Docker 没起来就不问**。体积是给"下载前判断磁盘够不够"用的；
          连镜像都导不进来的时候，这个判断没有意义。等环境就绪后
          `refresh_report()` 会再调一次，所以晚点开 Docker 也能补上。
        * **只问本机还没导入的那些**。已经装好的镜像不会再下载，
          知道精确体积没意义。已经导入 X5 CPU 的机器上这里只发 2~3 个请求。
        """
        if self._remote_probed or self.tasks.busy:
            return
        if not (self.report and self.report.docker.daemon_ok):
            return
        self._remote_probed = True          # 每个会话只自动问一次
        pending = [
            spec for spec in self.specs
            if not img.find_loaded(spec.image_ref_hint, self.local)
        ]
        if pending:
            self._probe_remote_async(specs=pending, silent=True)

    # ------------------------------------------------------------------
    # 下载并导入
    # ------------------------------------------------------------------
    def _on_fetch(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            # 下拉栏始终有选中项，走到这里说明镜像清单是空的（打包或数据文件出问题）
            self.log.append_line("[提示] 镜像清单为空，无法确定要下载哪个。")
            return
        if self.tasks.busy:
            self.log.append_line("[提示] 已有任务在跑，请先等待或取消。")
            return
        # 全局单飞：下载 + `docker load` 动辄几十分钟，不能和别的页面的
        # `docker run` 同时跑（进度条是全局共享的，两边会互相覆盖）
        if not self.jobs.claim(self, f"获取镜像 {spec.display_name}"):
            self.log.append_line(f"[提示] {self.jobs.blocked_message()}")
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
        self.jobs.release(self)          # 触发 changed → 按钮状态统一由它管
        self.progress.setValue(100)
        self.progress.setFormat(f"完成：{result.action}")
        self.log.append_line(f"[完成] {result.action} → {result.image_ref}")
        self.refresh_local()
        self.refresh_report()

    def _on_fetch_failed(self, message: str) -> None:
        self.jobs.release(self)
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
