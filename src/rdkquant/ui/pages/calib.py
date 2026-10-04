# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""S3 校准数据准备（官方 4.1.1.5）。

职责划分见 `core/calib.py`：容器里的探针只报事实，依赖配置的策略在那边——
页面负责把用户的选择传过去。

关于生成二进制校准数据，有一条**实测**得到的硬约束：产物必须是**无文件头的
裸字节**，长度恰好 `W×H×C×itemsize`。放 `.npy` 会失败——工具按裸字节读，
多出来的 128 字节文件头会被算进元素个数。详见 docs/design/container-findings.md。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.calib import (
    CalibBuildSpec,
    evaluate_probe,
    expected_hw_from_shape,
)
from ...core import calibpick
from ...core.calibfilter import (
    CalibStat,
    FilterSpec,
    apply_filter,
    duplicate_names,
    filter_and_sample,
    most_common_resolution,
)
from ...core.findings import LEVEL_ERROR, LEVEL_OK, Finding
from ...core.workflow import RunContext
from .. import calibimages
from ..metrics import METRICS
from ..widgets import Card, CollapsibleSection, MetricTile, Tone
from ..widgets.inputs import NoWheelComboBox, NoWheelDoubleSpinBox, NoWheelSpinBox
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
        #: 逐张统计的落地路径（**旧路径**，早已改由宿主机扫描直接产出，
#: 见 `ui/calibimages.py:scan()`）。留常量是因为探针那边仍然会写。
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
        # ⚠️ **刻意没有「曝光均值范围」磁贴。**
        #
        # 原来有一个，显示容器抽样 32 张算出的聚合区间（实测那份数据是
        # `max_near_black=0.7158`）。但那个数字**没法用**：
        #   1. 不知道是哪一张（只有 min/max，没有文件名）
        #   2. 筛不掉（FilterSpec 里没有曝光维度）
        #   3. 没有任何策略消费它（core/calib.py 里"曝光"只出现在 docstring）
        # 一个"没人能用、也没人用"的指标摆在界面上，比没有更糟 ——
        # 它让人以为程序检查过了。
        #
        # 改成**逐张**算、并且能用「排除过曝/欠曝」一键处理（见 _exclude_bad_exposure）。
        for tile in (self.tile_count, self.tile_res, self.tile_dup):
            tiles_line.addWidget(tile)
        overview.add(tiles)
        self.backend_label = self._hint_label("尚未扫描。")
        overview.add(self.backend_label)
        self.body.addWidget(overview)

        # ---- 筛选 + 缩略图 ----
        thumbs_card = Card("筛选与挑选", "缩略图随滚动生成，不占等待时间")

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
        #
        # ⚠️ **这里没有「预览张数」控件。** 曾经有一个（默认 50，均匀采样），
        # 它存在的理由是"批量生成缩略图太贵"（3504 张要 26.5 秒 + 35 MB）。
        # 缩略图改成**按需解码**之后那个理由没有了，控件却留着 ——
        # 于是它从性能妥协变成了**第二层看不见的筛选**：用户设完筛选条件，
        # 工具还替他决定"1172 张里你看哪 50 张"。
        #
        # 而且均匀采样对"挑选"这件事本身是错的：数据集按时间/场景排序，
        # 采样会把它打散 —— 某个场景有 20 张几乎一样的、你想要 2 张，
        # 采样可能给你 0 张，还看不到旁边的。连续浏览才能做这种判断。
        #
        # 现在**筛选结果全量进网格**。嫌多就把筛选条件收紧 ——
        # 漏斗那行会告诉你「筛选后 N 张」。

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
            ("排除过曝/欠曝", self._exclude_bad_exposure),
            ("自动挑选", self._on_auto_pick, ),
        ):
            picks.addWidget(self._button(text, slot))
        picks.addStretch(1)
        self.apply_btn = self._button(
            "应用筛选并刷新预览", self._on_apply_filter, primary=True,
            tooltip="筛选是纯本地计算；这一步只是按结果重装网格，不跑容器、不预生成缩略图。",
        )
        picks.addWidget(self.apply_btn)
        thumbs_card.add(pick_row)

        self.grid = ThumbnailGrid()
        self.grid.checkedChanged.connect(self._on_checked_changed)
        thumbs_card.add(self.grid)
        self.body.addWidget(thumbs_card)

        # ---- 参数 ----
        params = Card("校准参数", "这些值会写进第 4 步的 config.yaml")
        self.type_box = NoWheelComboBox()
        for value, label in CALIBRATION_TYPES:
            self.type_box.addItem(label, value)
        self.type_box.currentIndexChanged.connect(self._on_param_changed)
        params.add(self._labelled("校准方式 calibration_type", self.type_box))

        self.dtype_box = NoWheelComboBox()
        for value, label in CAL_DATA_TYPES:
            self.dtype_box.addItem(label, value)
        self.dtype_box.currentIndexChanged.connect(self._on_param_changed)
        params.add(self._labelled("数据类型 cal_data_type", self.dtype_box))

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
            # ⚠️ 筛选上下限用**不吃滚轮**的框：页面本身能滚，光标扫过就改掉
            # 一个筛选边界，而漏斗会立刻重算 —— 用户根本不知道发生了什么。
            if decimals:
                box = NoWheelDoubleSpinBox()
                box.setDecimals(decimals)
                box.setSingleStep(step)
            else:
                box = NoWheelSpinBox()
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

    @staticmethod
    def _stems(names) -> set[str]:
        """把文件名统一成不带扩展名的形式再比较。

        ⚠️ **必须这样比。** 网格里装的是**缩略图**（`0070.png`），而
        `_displayed` 存的是**源文件名**（`0070.jpg`）。直接拿两边做集合比较
        永远不相等 —— 实测症状就是：每次点「生成」都报
        「勾选与当前预览不一致，请重新勾选」，而用户根本无从改起。
        """
        return {Path(n).stem for n in names}

    def _display_names(self) -> list[str]:
        """筛选之后应当出现在网格里的名字。

        **全量，不截断、不采样。** `count=0` 就是"全都要"。
        上限（`MAX_TILES`）和采样张数（`count_box`）都已经删掉 ——
        前者因为装条目免费了，后者因为它变成了第二层看不见的筛选。
        """
        _result, names = filter_and_sample(
            self._stats, self.filter_spec(), count=0, duplicates=self._duplicates,
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
            self._stats, self.filter_spec(), count=0, duplicates=self._duplicates,
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
        # **没有「预览 N」这一级了**：筛选结果全量进网格，它和「筛选后 N」
        # 永远是同一个数。原来是两级（筛选 → 采样），现在只有一级。
        if self._displayed:
            parts.append(f"已选 {checked}")
        self.funnel_label.setText(" → ".join(parts))

        # 张数超过慢速线时明说 —— `default` / `mix` 是搜索策略，成本随样本数
        # 线性涨，而每张的成本又被数据难度放大（实测 60 倍）
        too_many = calibpick.slow_warning(checked)
        if too_many is not None and self._displayed:
            self.funnel_label.setText(
                self.funnel_label.text() + f"　⚠️ {too_many.message}"
            )

        if kept == 0 and self.filter_spec().active:
            self.funnel_label.setText(
                f"共 {total} 张 → 筛选后 0 张（筛掉：{result.explain()}）"
                "。放宽条件试试。"
            )
        if self._displayed and self._stems(names) != self._stems(self._displayed):
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

    def _exclude_bad_exposure(self) -> None:
        """把过曝 / 欠曝的那些取消勾选。

        官方要求排除过曝、欠曝的样本。曝光是在**缩略图解码时顺带算的**
        （像素已经解出来了，几乎免费），所以它**只覆盖已经滚过、解码过的那些**。

        这是能力上的**减弱**：原来批量生成时能覆盖整个预览集。换来的是不再
        为它解码全量（3504 张要 15 秒）。所以日志必须**如实说明覆盖了多少张** ——
        不能假装检查过全部，那正是被删掉的那个「曝光均值范围」磁贴的毛病。
        """
        exposure = self.grid.exposure_for(self._displayed)
        decoded = self.grid.decoded_rows()
        if not exposure:
            self.log.append_line(
                "[筛选] 还没有曝光数据 —— 曝光是缩略图解码时顺带算的，"
                "先在预览里滚动一下让图片加载出来。"
            )
            return

        bad = sorted(name for name, item in exposure.items() if item.is_bad)
        if not bad:
            self.log.append_line(
                f"[筛选] 已解码 {decoded} 张，其中没有明显过曝或欠曝的。"
                "（未解码的不在检查范围内）"
            )
            return

        removed = self.grid.uncheck(bad)
        samples = "、".join(
            f"{n}（均值 {exposure[n].mean:.0f}）" for n in bad[:3]
        )
        self.log.append_line(
            f"[筛选] 已解码 {decoded} 张，检出 {len(bad)} 张过曝/欠曝，"
            f"取消了其中 {removed} 张的勾选：{samples}"
            f"{' 等' if len(bad) > 3 else ''}（未解码的不在检查范围内）"
        )

    def _on_auto_pick(self) -> bool:
        """按官方口径自动挑选校准集。**返回任务有没有真的起来。**

        **为什么值得做。** 官方（X5 手册 6.3.3）要求 20~100 张、来自训练/验证集、
        **不要异常样本（纯色图片、不含目标的图片）**；社区口径是 >50 张校准会明显变慢。
        而"随手勾一批"或"均匀采样"两条都不满足。

        代价不是抽象的：实测同一份数据，**未经筛选**的图比正常图慢 **60 倍**
        （168 s/张 vs 2.7 s/张）—— `default` / `mix` 都是**搜索**策略，
        数据越"难"，升级得越深、敏感节点越多（官方 FAQ 6.6.1.4 / 6.6.1.5）。

        **只做机械可判的那部分**：去重、剔纯色、按亮度分层。
        「不含目标」判不了（要模型推理），所以预览必须留着 —— 用户还是得看一眼。
        """
        if not self._displayed:
            self.log.append_line("[挑选] 请先扫描并刷新预览。")
            return False
        source = Path(self.directory)
        names = list(self._displayed)

        def job(emit_line, emit_progress, cancel):
            emit_line(
                f"[挑选] 全量测量 {len(names)} 张（逐张解码，约十几秒）…"
            )

            def on_progress(done: int, total: int) -> None:
                # 测量占前 90%，剩下 10% 留给挑选本身（几毫秒）——
                # 全给测量的话进度条会停在 100% 之前就跳走
                emit_progress(int(done * 90 / max(1, total)), f"测量 {done}/{total}")

            candidates = calibimages.measure_candidates(
                source, names, on_progress=on_progress, cancel=cancel.is_set,
            )
            result = calibpick.pick(candidates, count=calibpick.TARGET)
            emit_progress(100, "挑选完成")
            return result

        self.start(job, self._on_auto_picked, label="挑选")
        return True

    def _on_auto_picked(self, result) -> None:
        """把挑选结果落到网格的勾选状态上。

        ⚠️ 名字要**按 stem 对齐**：候选是**源文件**名（`.jpg`），而网格里存的
        是缩略图名（`.png`）。直接比字符串会一张都匹配不上 —— 这个坑本项目
        踩过一次（「勾选与当前预览不一致」那条假告警）。
        """
        wanted = self._stems(result.chosen)
        names = [n for n in self.grid.all_names() if self._stems([n]) & wanted]
        picked = self.grid.check_only(names)

        self.log.append_line(
            f"[挑选] 从 {len(self.grid.all_names())} 张里挑了 {picked} 张"
            f"（目标 {calibpick.TARGET}，官方建议 20~100）"
        )
        if result.dropped_solid:
            self.log.append_line(
                f"[挑选] 剔除 {len(result.dropped_solid)} 张近纯色图："
                + "、".join(result.dropped_solid[:5])
            )
        if result.dropped_small:
            self.log.append_line(
                f"[挑选] 剔除 {len(result.dropped_small)} 张短边过小的图："
                + "、".join(result.dropped_small[:5])
                + "（小图拉到模型输入尺寸只会变糊）"
            )
        if result.dropped_duplicate:
            self.log.append_line(
                f"[挑选] 剔除 {len(result.dropped_duplicate)} 张近似重复帧："
                + "、".join(result.dropped_duplicate[:5])
            )
        for bucket in result.buckets:
            if bucket.pool == 0 and bucket.quota:
                self.log.append_line(
                    f"[挑选] 亮度 {bucket.low}~{bucket.high} 这个区间**一张都没有** —— "
                    "数据集本身缺这一段，不是筛选剔掉的。"
                )
        self.set_findings(result.findings)

    def _on_checked_changed(self) -> None:
        if not self._ready:
            return
        self._refresh_funnel()
        self.envChanged.emit()

    def _on_apply_filter(self) -> None:
        """用当前筛选 + 采样结果刷新预览。

        **只重装条目，不预生成缩略图。** 网格按需解码（`widgets/thumbnailgrid.py`）：
        装 3504 个条目是免费的，真正解码只发生在格子滚到可见时。
        原来要在容器里批量生成 —— 3504 张 26.5 秒 + 35 MB，而当时还只能靠
        `MAX_TILES = 200` 把数量压住。
        """
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

        # **不再预生成缩略图。** 网格按需解码（见 widgets/thumbnailgrid.py）——
        # 装 3504 个条目是免费的，真正解码只发生在格子滚到可见时。
        # 原来这一步要在容器里批量生成，3504 张要 26.5 秒 + 35 MB。
        self._displayed = list(names)
        self.grid.set_images(staged, names=names)
        self.log.append_line(
            f"[筛选] 预览已刷新：{len(names)} 张（缩略图随滚动生成）"
        )
        # **把网格滚进视野。** 卡片栈到网格上面已经有 500 多像素，而网格固定
        # 4 行高 —— 不滚的话用户点完「应用筛选」，结果在视野外，看着像没反应。
        self.scroll.ensureWidgetVisible(self.grid, 0, 0)
        self._refresh_funnel()
        self.envChanged.emit()

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

        ⚠️ **目标目录要先清空，是"替换"不是"合并"。** 实测踩过：工作区里躺着
        上一次的 **3504 张**，而新选的 50 张**文件同名**（它们本来就是从那份数据里
        挑出来的），于是 50 张全部覆盖到同名旧文件上，目录里**仍然是 3504 张** ——
        用户以为自己只选了 50 张。当时日志还写着"已收进工作区：50 张"，
        更坐实了这个误解：那句话数的是**处理了几个**，不是**目录里现在有几个**。

        合并还有一个更隐蔽的坏处：先扫 A 数据集再扫 B 数据集，目录里是**两个
        数据集的和**，而界面上只显示了 B。替换就没这个问题。
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

        stale = [p for p in target.iterdir() if p.is_file()]
        if stale:
            emit_line(
                f"[校准] 清掉工作区里上一次的 {len(stale)} 张"
                "（目标目录是替换而不是累加）"
            )
            for path in stale:
                if cancel.is_set():
                    emit_line("[校准] 已取消")
                    return None
                path.unlink()

        emit_line(f"[校准] 正在把 {len(pending)} 张图片收进工作区（cal_images/）…")
        for index, path in enumerate(pending, start=1):
            if cancel.is_set():
                emit_line("[校准] 已取消")
                return None
            shutil.copy2(path, target / path.name)
            emit_progress(int(index * 100 / len(pending)), f"复制 {index}/{len(pending)}")

        # 收完**数一遍**再报。报"复制了几个"和报"目录里现在有几个"是两件事 ——
        # 混在一起正是上一个 bug 让人看不出来的原因。
        landed = sum(1 for p in target.iterdir() if p.is_file())
        emit_line(f"[校准] 已收进工作区：{landed} 张 → cal_images/")
        if landed != len(pending):
            emit_line(
                f"[校准] ⚠️ 目录里的张数（{landed}）和源目录（{len(pending)}）对不上"
            )
        return target

    @staticmethod
    def _as_payload(result, staged) -> dict:
        """把宿主扫描结果拼成**和探针回包同形状**的 dict。

        这样 `core/calib.py` 的判定逻辑（`evaluate_probe`）一行都不用改 ——
        它只认这个形状。换数据来源不该顺带换判定逻辑。
        """
        counts: dict[tuple[int, int], int] = {}
        for stat in result.stats:
            if stat.width and stat.height:
                key = (stat.width, stat.height)
                counts[key] = counts.get(key, 0) + 1
        resolutions = [
            {"width": w, "height": h, "count": c}
            for (w, h), c in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        return {
            "ok": True,
            "directory": str(staged),
            "count": result.total,
            "image_like": result.image_like,
            "binary_like": result.binary_like,
            "resolutions": resolutions,
            "duplicates": result.duplicates,
            # 曝光不再来自这里：改成逐张、在生成缩略图时算（见 _exposure）
            "exposure": None,
            "problems": [],
            "warnings": [
                f"{len(result.unreadable)} 张读不出尺寸（格式不支持或文件损坏）"
            ] if result.unreadable else [],
            "_staged": str(staged),
        }

    def _on_scan(self) -> None:
        """扫描校准目录。**在宿主机上做，不跑容器。**

        原来这一步要 `docker run`，因为宿主机"没有图像库"。实测 3504 张：
        容器 18.0 秒 → 宿主机文件头解析 **0.6 秒**（32x），而且模式、宽高
        与容器侧 PIL 逐张零不一致。

        真正需要容器的只剩「生成校准数据」那一步（字节契约在消费它的
        环境里校验）。
        """
        source = Path(self.directory)

        def job(emit_line, emit_progress, cancel):
            staged = self._stage(source, emit_line, emit_progress, cancel)
            if staged is None:
                return {"ok": False, "error": f"目录不可用：{source}"}

            def on_progress(done: int, total: int) -> None:
                emit_progress(int(done * 100 / max(1, total)), f"扫描 {done}/{total}")

            emit_line(f"[校准] 扫描 {staged}")
            result = calibimages.scan(
                staged, on_progress=on_progress, cancel=cancel.is_set,
            )
            emit_line(
                f"[校准] 共 {result.total} 张（图片 {result.image_like}、"
                f"二进制 {result.binary_like}），重复 {len(result.duplicates)} 组"
            )
            payload = self._as_payload(result, staged)
            payload["_stats"] = result.stats
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

        # 逐张统计来自**宿主扫描**（`calibimages.scan`），不再是
        # `<ws>/.rdkquant/calib-stats.json`。全量、不受回包 500 条上限影响。
        self._stats = list(payload.get("_stats") or [])
        # 上一次失败可能把筛选区禁用了，成功时要恢复 —— 否则修好之后
        # 界面还是灰的，用户会以为功能坏了（踩过）
        self.filter_section.setEnabled(bool(self._stats))
        if not self._stats:
            self.log.append_line("[筛选] 没有读到逐张统计，筛选已禁用")

        self._evaluate()
        names = self._display_names()
        # 直接把**源图目录**交给网格。缩略图由它按需解码 ——
        # 这里不再预生成任何东西，装 3504 张也是瞬间的。
        staged = self._staged or (self.workspace / "cal_images")
        if names:
            self.grid.set_images(staged, names=names)
            self._displayed = list(names)
            self.scroll.ensureWidgetVisible(self.grid, 0, 0)
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

        # 容器那份抽样 32 张的 exposure 聚合值**不再显示**（原因见上面磁贴处的说明）。
        # 逐张曝光由网格在解码缩略图时顺带算，见 `ThumbnailGrid.exposure_for()`。

        if payload.get("ok") is False:
            self.backend_label.setText(f"扫描失败：{payload.get('error')}")
        else:
            # ⚠️ 这里原来读容器回包里的 `imaging_backend`。扫描搬到宿主机之后
            # 那个字段没有了，于是会掉进最后那个分支，对**每个用户**显示
            # 「容器里没有可用的图像库」—— 既错又吓人（截图核对时抓到的）。
            self.backend_label.setText(
                f"已在宿主机上逐个读取 {payload.get('count', 0)} 张的尺寸与色彩模式。"
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
        if self._displayed and self._stems(expected) != self._stems(self._displayed):
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
        # 按**不带扩展名**的形式比：网格里是缩略图 `0070.png`，
        # 而 `_displayed` 是源文件名 `0070.jpg`（见 _stems 的说明）
        if self._displayed and not self._stems(checked) <= self._stems(self._displayed):
            self.log.append_line("[校准] 勾选与当前预览不一致，请重新勾选。")
            return

        spec = CalibBuildSpec(
            shape=shape,
            layout="NCHW",
            color="bgr",
            dtype=self.dtype_box.currentData(),
            # `only=checked` 已经**明确指定**了要哪些，所以 count 给 0（全都要）。
            # 这里原来是 `count=self.count_box.value()` —— 控件删掉后那行会
            # AttributeError，而且它本来就被 `only` 盖过。
            count=0,
            # **明确指定勾选的那些**：用户的选择优先于任何自动采样
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
