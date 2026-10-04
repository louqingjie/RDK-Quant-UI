# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""校准集挑选策略的测试。

这块是**纯策略**，不碰图像也不碰 Qt —— 图像解码在 `ui/calibimages.py`，
结论在这里下。所以这里的用例可以放心地只喂数字。
"""

from __future__ import annotations

from rdkquant.core.calibpick import (
    BUCKETS,
    MIN_SIDE,
    SLOW_THRESHOLD,
    SOLID_SPREAD,
    TARGET,
    Candidate,
    pick,
    quotas,
    slow_warning,
)


def make(name: str, brightness: float, spread: float = 60.0, **kwargs) -> Candidate:
    # 默认给一个"够大"的尺寸 —— 尺寸过滤单测在下面，别让所有用例都踩到它
    kwargs.setdefault("width", 1280)
    kwargs.setdefault("height", 720)
    return Candidate(name=name, brightness=brightness, spread=spread, **kwargs)


def ramp(count: int, low: float = 0.0, high: float = 255.0) -> list[Candidate]:
    """造一批亮度均匀铺开的候选图。"""
    if count == 1:
        values = [(low + high) / 2]
    else:
        step = (high - low) / (count - 1)
        values = [low + i * step for i in range(count)]
    return [make(f"{i:04d}.jpg", v) for i, v in enumerate(values)]


# --------------------------------------------------------------------------
# quotas
# --------------------------------------------------------------------------
def test_quotas_sum_to_the_target():
    for count in (1, 7, 20, 50, 100, 200, 380):
        assert sum(quotas(count)) == count, count


def test_quotas_scale_proportionally():
    """权重比要保住 —— 这是从 RDK-BPU 那套真实数据分布搬过来的。"""
    at_200 = quotas(200)
    at_50 = quotas(50)
    # 最大的那个桶（50~80）在两种目标下都该拿最多
    assert at_200.index(max(at_200)) == at_50.index(max(at_50)) == 3


def test_every_bucket_gets_at_least_one_when_count_allows():
    """**保底 1 张**：纯等比会把权重最小的桶抹成 0，两端就没人了。

    权重来自真实数据集（50~120 占大头），最亮桶权重只有 2/200 ——
    目标 50 张时等比配额是 0.5，直接归零。
    """
    plan = quotas(50)
    assert all(q >= 1 for q in plan), f"有桶被抹成 0：{plan}"
    assert sum(plan) == 50


def test_quotas_edge_cases():
    assert sum(quotas(1)) == 1
    assert quotas(0) == [0] * len(BUCKETS)
    assert len(quotas(50)) == len(BUCKETS)


def test_quotas_handles_zero_weights():
    assert sum(quotas(5, ((0, 1, 0), (1, 2, 0)))) == 0


# --------------------------------------------------------------------------
# 剔除
# --------------------------------------------------------------------------
def test_solid_images_are_dropped():
    """**官方点名的「纯色图片」。** 灰度标准差判它 —— 两端占比判不出来。"""
    items = [
        make("solid_black.jpg", brightness=0.0, spread=0.0),
        make("solid_gray.jpg", brightness=128.0, spread=0.5),
        make("normal.jpg", brightness=128.0, spread=60.0),
    ]
    result = pick(items, count=3)
    assert result.chosen == ("normal.jpg",)
    assert set(result.dropped_solid) == {"solid_black.jpg", "solid_gray.jpg"}
    finding = next(f for f in result.findings if f.code == "PICK_DROPPED_SOLID")
    assert "纯色" in finding.message


def test_the_solid_threshold_is_conservative():
    """实测正常暗光图的标准差是 14~31。阈值必须远低于它，否则会误伤。"""
    assert SOLID_SPREAD < 14
    dark_but_real = make("dark.jpg", brightness=20.0, spread=14.0)
    assert not dark_but_real.is_solid


def test_duplicates_are_dropped_by_fingerprint():
    items = [
        make("a.jpg", 100.0, fingerprint="aaaa"),
        make("b.jpg", 110.0, fingerprint="aaaa"),
        make("c.jpg", 120.0, fingerprint="cccc"),
    ]
    result = pick(items, count=3)
    assert result.chosen == ("a.jpg", "c.jpg")
    assert result.dropped_duplicate == ("b.jpg",)


def test_small_images_are_dropped():
    """短边过小的图要剔除 —— 拉到模型输入尺寸只会变糊。

    参考实现（`RDK-BPU/scripts/prepare_calibration_data.py`）同样有这条。
    实测本项目那份数据集：短边中位数 384，取 360 保留 69.3%。
    """
    items = [
        make("tiny.jpg", 100.0, width=52, height=49),
        make("big.jpg", 100.0, width=1920, height=1080),
        make("square.jpg", 100.0, width=640, height=640),
    ]
    result = pick(items, count=3)
    assert "tiny.jpg" not in result.chosen
    assert result.dropped_small == ("tiny.jpg",)
    assert set(result.chosen) == {"big.jpg", "square.jpg"}
    finding = next(f for f in result.findings if f.code == "PICK_DROPPED_SMALL")
    assert str(MIN_SIDE) in finding.message


def test_min_side_is_exactly_the_boundary():
    """刚好等于阈值的要留下 —— 边界写成 `<` 还是 `<=` 差一张图。"""
    items = [make("edge.jpg", 100.0, width=MIN_SIDE, height=MIN_SIDE),
             make("under.jpg", 100.0, width=MIN_SIDE - 1, height=900)]
    result = pick(items, count=2)
    assert result.chosen == ("edge.jpg",)
    assert result.dropped_small == ("under.jpg",)


def test_min_side_is_configurable():
    items = [make("m.jpg", 100.0, width=200, height=200)]
    assert pick(items, count=1).dropped_small == ("m.jpg",)
    assert pick(items, count=1, min_side=100).dropped_small == ()


def test_unmeasured_size_counts_as_too_small():
    """宽高没读出来（0）的，不能蒙混过关 —— 它无法被校验。"""
    item = Candidate(name="x.jpg", brightness=100.0, spread=60.0)
    assert item.min_side == 0
    assert item.is_small()
    assert pick([item], count=1).dropped_small == ("x.jpg",)


def test_missing_fingerprint_never_dedupes():
    """没算指纹就不去重 —— 空字符串不是"同一张图"。"""
    items = [make("a.jpg", 100.0), make("b.jpg", 120.0)]
    assert len(pick(items, count=2).chosen) == 2


# --------------------------------------------------------------------------
# 分层抽样
# --------------------------------------------------------------------------
def test_covers_both_dark_and_bright_ends():
    """官方要求覆盖典型场景；分桶的意义就是**别只挑中间的**。"""
    result = pick(ramp(300), count=50)
    picked = {c.name for c in _lookup(ramp(300), result.chosen)}
    brightness = [c.brightness for c in _lookup(ramp(300), result.chosen)]
    assert min(brightness) < 15, f"最暗的没进来：{min(brightness)}"
    assert max(brightness) > 180, f"最亮的没进来：{max(brightness)}"
    assert len(picked) == result.count


def test_does_not_just_take_the_first_n():
    """按名字取前 N 张是最糟的做法 —— 那正是我们踩过的坑。"""
    items = ramp(300)
    result = pick(items, count=20)
    first_20 = {c.name for c in sorted(items, key=lambda c: c.name)[:20]}
    assert set(result.chosen) != first_20


def test_pick_is_deterministic():
    """同样的输入必须给同样的结果，用户对照清单时不会"每次跑都不一样"。"""
    items = ramp(200)
    assert pick(items, 30).chosen == pick(items, 30).chosen


def test_shortfall_is_redistributed():
    """所有图都挤在一个桶里时，缺额要转给有货的桶，而不是静默少给。"""
    items = [make(f"{i:04d}.jpg", 60.0) for i in range(40)]
    result = pick(items, count=30)
    assert result.count == 30, "桶不均衡时不该少挑"


def test_reports_the_shortfall_when_the_pool_is_simply_too_small():
    items = ramp(5)
    result = pick(items, count=50)
    assert result.count == 5
    assert any(f.code == "PICK_SHORT" for f in result.findings)


def test_bucket_report_has_one_row_per_bucket():
    result = pick(ramp(300), count=50)
    assert len(result.buckets) == len(BUCKETS)
    assert sum(b.chosen for b in result.buckets) == result.count


def test_choice_is_sorted_by_name():
    result = pick(ramp(300), count=30)
    assert list(result.chosen) == sorted(result.chosen)


def test_empty_input_is_not_an_error():
    result = pick([], count=50)
    assert result.count == 0
    assert any(f.code == "PICK_EMPTY" for f in result.findings)


def _lookup(items, names):
    by_name = {c.name: c for c in items}
    return [by_name[name] for name in names]


# --------------------------------------------------------------------------
# 慢速提醒
# --------------------------------------------------------------------------
def test_slow_warning_fires_above_the_threshold():
    """用户选了 380 张时，界面必须说话。"""
    finding = slow_warning(380)
    assert finding is not None
    assert "380" in finding.message
    assert str(SLOW_THRESHOLD) in finding.message
    assert "自动挑选" in finding.hint


def test_slow_warning_is_quiet_at_or_below_the_threshold():
    assert slow_warning(SLOW_THRESHOLD) is None
    assert slow_warning(20) is None
    assert slow_warning(0) is None


def test_target_is_inside_the_official_range():
    """官方区间是 20~100（手册 6.3.3）。目标值必须落在里面。"""
    assert 20 <= TARGET <= 100
    assert TARGET <= SLOW_THRESHOLD
