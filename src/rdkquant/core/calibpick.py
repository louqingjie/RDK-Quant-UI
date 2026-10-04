# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准集挑选策略 —— 官方口径的落地。

## 为什么需要它

实测：同一次转换，喂**未经筛选**的图比喂正常图慢 **60 倍**（168 s/张 vs 2.7 s/张）。
原因是 `default` / `mix` 都是**搜索**策略，而搜索的深度由数据决定：

* `default` 按余弦相似度是否达到 0.995 决定要不要升级到下一步（官方 FAQ 6.6.1.4）
* `mix` 先算**每个节点的量化敏感度**，再**逐敏感节点**试三种方法（官方 FAQ 6.6.1.5）

数据越"难"，升级得越深、敏感节点越多，成本成倍涨。喂进去一堆异常样本就是
在主动触发最贵的路径。

## 官方口径

X5 工具链手册 6.3.3：

> 校准阶段会需要 **20~100 份**的标定样本……我们希望这些校准样本来自于您训练
> 模型使用的训练集或验证集，**不要使用非常少见的异常样本，例如纯色图片、
> 不含任何检测或分类目标的图片等**。

社区实践（RDK X5 PTQ 指南，引 Model Zoo 的警告阈值）：**20 张起步；>50 张
校准会很慢**。

## 本模块做什么

**只做能机械判定的那部分**：剔除纯色图（用灰度标准差）、去重（用指纹）、
按亮度分桶分层抽样。

**「不含目标」判定不了** —— 那需要模型推理。这类图只能靠用户自己看预览排除，
所以界面上必须留着预览，不能拿"自动挑选"当替身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .findings import LEVEL_OK, LEVEL_WARN, Finding

#: 目标张数。
#:
#: 官方区间是 20~100，但社区实测 **>50 就明显变慢**，所以取中间偏下的 50：
#: 既在官方区间里，又不踩社区的慢速线。
TARGET = 50

#: 超过这个数就提醒。比 `TARGET` 高，是因为用户可能有自己的理由多选几张。
SLOW_THRESHOLD = 50

#: 灰度标准差低于它就算「纯色图」。**保守取值**：
#: 实测正常暗光图的标准差在 14~31，纯色图接近 0，中间留了很宽的余量。
#: 宁可漏掉边缘样本，也不要误伤正常图 —— 误排除会悄悄缩小校准集。
SOLID_SPREAD = 6.0

#: 短边低于它就剔除。
#:
#: 理由：校准数据最终要被缩放到模型输入尺寸（常见 640×640）。一张 52×49 的图
#: 拉上去是 12 倍放大，得到的只是一团糊 —— 它既不代表真实分布，又会把校准统计
#: 带偏。参考实现（`RDK-BPU/scripts/prepare_calibration_data.py`）同样有这条，
#: 默认值也是 360。
#:
#: 在本项目那份 3504 张的数据集上实测：短边中位数 384，取 360 保留 **69.3%** ——
#: 不激进。阈值是**数据集相关**的，所以它是参数；这里只是一个起点。
MIN_SIDE = 360

#: 亮度分桶与**相对权重**（0~255）。
#:
#: 直接照搬 `RDK-BPU/scripts/prepare_calibration_data.py` 的分桶比例 ——
#: 那套是在真实数据集上跑出来的，比我自己编的分布靠谱。权重总和 200，
#: 实际配额按目标张数等比缩放（见 `quotas`）。
BUCKETS: tuple[tuple[int, int, int], ...] = (
    (0, 15, 15),
    (15, 30, 20),
    (30, 50, 55),
    (50, 80, 65),
    (80, 120, 35),
    (120, 180, 8),
    (180, 256, 2),
)


@dataclass(frozen=True)
class Candidate:
    """一张候选图**测出来的事实**。不含结论 —— 结论在 `pick()` 里下。"""

    name: str
    #: 灰度均值 0~255
    brightness: float
    #: 灰度标准差。判「纯色」用它 —— `near_black`/`near_white` 占比判不出来：
    #: 一张整片 128 灰的图两端占比都是 0，但正是官方点名的"纯色图片"。
    spread: float
    #: 感知哈希（aHash）。空字符串表示没算，那时不去重。
    fingerprint: str = ""
    width: int = 0
    height: int = 0

    @property
    def is_solid(self) -> bool:
        return self.spread < SOLID_SPREAD

    @property
    def min_side(self) -> int:
        """短边。宽高有一个是 0（没量到）时返回 0 —— 那会被判为过小。"""
        if self.width <= 0 or self.height <= 0:
            return 0
        return min(self.width, self.height)

    def is_small(self, min_side: int = MIN_SIDE) -> bool:
        return self.min_side < min_side


@dataclass(frozen=True)
class BucketReport:
    low: int
    high: int
    pool: int          # 这个桶里有多少张可用
    chosen: int        # 实际选了几张
    quota: int         # 计划选几张


@dataclass(frozen=True)
class PickResult:
    chosen: tuple[str, ...]
    buckets: tuple[BucketReport, ...]
    dropped_solid: tuple[str, ...]
    dropped_duplicate: tuple[str, ...]
    dropped_small: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def count(self) -> int:
        return len(self.chosen)


def quotas(count: int, buckets: Sequence[tuple[int, int, int]] = BUCKETS) -> list[int]:
    """把相对权重按 `count` 缩放成整数配额。

    **每个有效桶先保底 1 张，剩下的才按权重分。**

    为什么不能纯等比：权重是从真实数据集搬来的（总 200），最亮那个桶
    （180~256）权重只有 2，目标 50 张时配额是 `50 × 2/200 = 0.5` —— 直接归零，
    **最亮端一张都进不来**。而分层的全部意义就是覆盖两端，抹掉它等于没分层。
    （实测：纯等比写法下 `count=50` 挑出来的最亮一张只有 179.9，
    刚好卡在 180 桶外面。）
    """
    weights = [max(0, weight) for _low, _high, weight in buckets]
    active = [index for index, weight in enumerate(weights) if weight > 0]
    out = [0] * len(buckets)
    if count <= 0 or not active:
        return out

    for index in active[:count]:
        out[index] = 1
    left = count - min(count, len(active))
    if left <= 0:
        return out

    total = sum(weights[index] for index in active)
    exact = {index: left * weights[index] / total for index in active}
    for index in active:
        out[index] += int(exact[index])

    remainder = count - sum(out)
    order = sorted(active, key=lambda i: exact[i] - int(exact[i]), reverse=True)
    for index in order[:remainder]:
        out[index] += 1
    return out


def _spread_evenly(pool: Sequence[Candidate], take: int) -> list[Candidate]:
    """从池子里**跨步均匀**取 `take` 张（按名字排序后）。

    均匀而不是随机：结果可复现，用户对照 manifest 时不会"每次跑都不一样"。
    """
    if take >= len(pool):
        return list(pool)
    if take <= 0:
        return []
    # 与 numpy.linspace(0, n-1, take) 等价，但只用标准库
    span = len(pool) - 1
    indexes = sorted({round(i * span / (take - 1)) if take > 1 else 0
                      for i in range(take)})
    return [pool[i] for i in indexes]


def pick(
    candidates: Sequence[Candidate],
    count: int = TARGET,
    *,
    buckets: Sequence[tuple[int, int, int]] = BUCKETS,
    min_side: int = MIN_SIDE,
) -> PickResult:
    """按官方口径挑校准集。

    顺序：**去重 → 剔小图 → 剔纯色 → 按亮度分桶分层抽样**。

    去重放在最前：重复帧往往就是同一张图，先去掉能让后面的桶统计少受干扰。
    小图放在纯色之前：尺寸是可硬判的事实，先按它筛一道最省事。

    某个桶不够时，缺额会**转给还有余量的桶**（按权重从大到小），
    尽量凑到目标张数 —— 而不是像参考脚本那样静默少给几张。
    """
    findings: list[Finding] = []

    # ---- 1) 去重 ----
    seen: set[str] = set()
    unique: list[Candidate] = []
    dropped_dup: list[str] = []
    for item in candidates:
        if item.fingerprint:
            if item.fingerprint in seen:
                dropped_dup.append(item.name)
                continue
            seen.add(item.fingerprint)
        unique.append(item)

    # ---- 2) 剔小图 ----
    dropped_small = [c.name for c in unique if c.is_small(min_side)]
    sized = [c for c in unique if not c.is_small(min_side)]

    # ---- 3) 剔纯色 ----
    dropped_solid = [c.name for c in sized if c.is_solid]
    usable = [c for c in sized if not c.is_solid]

    # ---- 3) 分桶 ----
    plan = quotas(count, buckets)
    pools: list[list[Candidate]] = []
    for low, high, _weight in buckets:
        pools.append(sorted((c for c in usable if low <= c.brightness < high),
                            key=lambda c: c.name))

    chosen: list[Candidate] = []
    taken: list[int] = []
    for pool, quota in zip(pools, plan):
        picked = _spread_evenly(pool, quota)
        chosen.extend(picked)
        taken.append(len(picked))

    # ---- 4) 缺额回流 ----
    shortfall = count - len(chosen)
    if shortfall > 0:
        # 还有余量的桶，按权重大小依次补
        order = sorted(range(len(buckets)), key=lambda i: buckets[i][2], reverse=True)
        for index in order:
            if shortfall <= 0:
                break
            left = [c for c in pools[index] if c not in chosen]
            if not left:
                continue
            extra = _spread_evenly(left, min(shortfall, len(left)))
            chosen.extend(extra)
            taken[index] += len(extra)
            shortfall -= len(extra)

    chosen.sort(key=lambda c: c.name)

    # ---- 结论 ----
    reports = tuple(
        BucketReport(low=low, high=high, pool=len(pools[index]),
                     chosen=taken[index], quota=plan[index])
        for index, (low, high, _weight) in enumerate(buckets)
    )

    if dropped_small:
        findings.append(Finding(
            LEVEL_WARN, "PICK_DROPPED_SMALL",
            f"剔除了 {len(dropped_small)} 张短边 < {min_side} 的小图",
            "小图拉到模型输入尺寸只会变糊，不代表真实分布。"
            "例：" + "、".join(dropped_small[:5]),
        ))
    if dropped_solid:
        findings.append(Finding(
            LEVEL_WARN, "PICK_DROPPED_SOLID",
            f"剔除了 {len(dropped_solid)} 张近纯色图（灰度标准差 < {SOLID_SPREAD:g}）",
            "官方明确要求不要用纯色图片做校准样本。"
            "例：" + "、".join(dropped_solid[:5]),
        ))
    if dropped_dup:
        findings.append(Finding(
            LEVEL_WARN, "PICK_DROPPED_DUPLICATE",
            f"剔除了 {len(dropped_dup)} 张近似重复帧",
            "重复帧会让校准统计偏向同一个场景。例：" + "、".join(dropped_dup[:5]),
        ))

    if not chosen:
        findings.append(Finding(
            LEVEL_OK if not candidates else LEVEL_WARN, "PICK_EMPTY",
            "没有可用的候选图", "先扫描校准目录。",
        ))
    elif len(chosen) < count:
        findings.append(Finding(
            LEVEL_WARN, "PICK_SHORT",
            f"只挑到 {len(chosen)} 张（目标 {count}）",
            "可用图不够。可以放宽筛选条件，或者补一些图。",
        ))
    else:
        findings.append(Finding(
            LEVEL_OK, "PICK_OK", f"挑了 {len(chosen)} 张校准图",
            "按亮度分桶分层抽样，暗光和强光两端都有覆盖。",
        ))

    return PickResult(
        chosen=tuple(c.name for c in chosen),
        buckets=reports,
        dropped_solid=tuple(dropped_solid),
        dropped_duplicate=tuple(dropped_dup),
        dropped_small=tuple(dropped_small),
        findings=tuple(findings),
    )


def slow_warning(count: int) -> Finding | None:
    """张数超过慢速线时给一条提醒。

    `default` / `mix` 是**搜索**策略，成本随样本数线性增长，而每张的成本又
    被数据难度放大（实测 60 倍）。社区口径是 **>50 就很慢**。
    """
    if count <= SLOW_THRESHOLD:
        return None
    return Finding(
        LEVEL_WARN, "CALIB_TOO_MANY",
        f"选了 {count} 张，超过 {SLOW_THRESHOLD} 张后校准会明显变慢",
        f"官方建议 20~100 张，社区实测 >{SLOW_THRESHOLD} 就慢。"
        f"点「自动挑选」可以按官方口径降到 {TARGET} 张左右。",
    )
