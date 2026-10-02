#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors
#
# ⚠️ 这个文件**不是**被 rdkquant 包导入的模块，而是**被拷进容器里执行**的独立脚本。
#    因此它有两条硬约束，改动时必须守住：
#
#    1. **Python 3.8 语法兼容**（RDK X3 的 OpenExplorer 容器是 3.8）。
#       不能用 X | Y 联合类型（注解里有 from __future__ import annotations 兜着，
#       但运行时位置不行）、不能用 match、不能用 dataclass(slots=True)。
#    2. **不依赖 rdkquant 包**，只用标准库 + 容器自带的第三方库。
#
#    协议：stdout 的最后一行永远是单行 JSON。宿主只解析最后一行。
#    任何异常都被捕获并转成 {"ok": false, "error": ...}，退出码仍为 0 ——
#    这样宿主永远不需要解析 traceback。只有脚本压根没跑起来才会非 0 退出。
#
# 为什么要把它放进容器：ONNX 图结构内省、图像测量、二进制校准数据生成，这些
# 都需要 onnx / 图像库。放在容器里做，宿主就**一个第三方依赖都不需要**，
# 而且我们看到的输入形状与 hb_mapper 看到的必然一致（同一个 Python、同一套库）。
#
# ── 职责边界（重要）─────────────────────────────────────────────────────
# 探针只负责**报告事实**和那些与环境无关的硬错误（目录为空、纯黑纯白图、
# 二进制字节数对不上）。**依赖配置的策略判断留在宿主**，例如
# "分辨率不一致 + preprocess_on=False 才算问题"——因为 preprocess_on 是
# 宿主侧的用户选择，探针不该知道它。
#
# `ok` 的含义是"探针跑通了并产出了数据"，**不是**"用户的东西没问题"。
# 后者看 problems / warnings 数组。只有探针自己跑不下去（没有图像库、
# 文件读不了）才 ok=false 并带 error。
# ────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import array
import hashlib
import json
import os
import sys
import traceback

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp",
              ".ppm", ".pgm", ".pbm", ".gif")
BINARY_EXTS = (".bin", ".dat", ".raw", ".f32", ".u8")

#: 扫描时最多返回多少个文件的明细（统计仍是全量），避免 JSON 过大
MAX_FILE_DETAILS = 500
#: 曝光统计最多采样多少张
EXPOSURE_SAMPLE = 32


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def emit(payload: dict) -> None:
    """打印协议行。

    `ensure_ascii=True` 是刻意的：JSON 里所有非 ASCII（我们的提示语都是中文）
    会被转义成 \\uXXXX，于是整份输出是纯 ASCII，**任何编码环境下都不可能损坏**。
    宿主解析之后拿到的仍是正确的中文。这个坑实测踩到过——在 Windows 上直接跑
    探针时，控制台码页把中文 JSON 打成了乱码。
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    """普通日志行。宿主只取最后一行 JSON，所以这些行随便打。

    刻意只输出 ASCII —— 需要给人看的中文信息一律走 JSON 字段。
    """
    sys.stdout.write(str(message) + "\n")
    sys.stdout.flush()


def finish(payload: dict) -> int:
    emit(payload)
    return 0


def fail(message: str, tb: str | None = None) -> int:
    payload = {"ok": False, "error": str(message)}
    if tb:
        payload["traceback"] = tb
    return finish(payload)


def progress(done: int, total: int) -> None:
    """进度行走 stderr 之外的行，宿主按前缀识别。"""
    sys.stdout.write("PROGRESS %d %d\n" % (done, total))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 图像后端探测
# ---------------------------------------------------------------------------
def _import_pil():
    from PIL import Image  # noqa: F401
    return "PIL"


def _import_skimage():
    import skimage.io  # noqa: F401
    return "skimage"


def _import_cv2():
    import cv2  # noqa: F401
    return "cv2"


BACKENDS = (("PIL", _import_pil), ("skimage", _import_skimage), ("cv2", _import_cv2))


def detect_imaging():
    """按 PIL → skimage → cv2 顺序探测。返回后端名，都没有则 None。

    刻意不假设容器里有什么——我没法在开发机上验证镜像内容，所以让它在
    目标环境里自己报。
    """
    for name, loader in BACKENDS:
        try:
            loader()
            return name
        except Exception:
            continue
    return None


def _pil_image(path):
    from PIL import Image

    return Image.open(path)


def image_size(path, backend):
    """返回 (width, height, mode)。读不出来返回 (None, None, None)。"""
    try:
        if backend == "PIL":
            with _pil_image(path) as im:
                return int(im.size[0]), int(im.size[1]), str(im.mode)
        if backend == "cv2":
            import cv2

            im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if im is None:
                return None, None, None
            h, w = im.shape[0], im.shape[1]
            return int(w), int(h), "cv2"
        if backend == "skimage":
            import skimage.io

            arr = skimage.io.imread(path)
            if arr.ndim < 2:
                return None, None, None
            return int(arr.shape[1]), int(arr.shape[0]), "skimage"
    except Exception:
        return None, None, None
    return None, None, None


def _pil_gray_stats(im):
    """返回 (mean, near_black_ratio, near_white_ratio)。用 256 桶直方图，精确且快。"""
    gray = im.convert("L")
    hist = gray.histogram()
    total = float(sum(hist)) or 1.0
    mean = sum(i * c for i, c in enumerate(hist)) / total
    near_black = sum(hist[0:3]) / total
    near_white = sum(hist[253:256]) / total
    return mean, near_black, near_white


def exposure_stats(path, backend):
    try:
        if backend == "PIL":
            with _pil_image(path) as im:
                return _pil_gray_stats(im)
        if backend == "cv2":
            import cv2
            import numpy as np

            im = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if im is None:
                return None
            hist = cv2.calcHist([im], [0], None, [256], [0, 256]).ravel()
            total = float(hist.sum()) or 1.0
            mean = float((np.arange(256) * hist).sum() / total)
            return mean, float(hist[0:3].sum() / total), float(hist[253:256].sum() / total)
        if backend == "skimage":
            import numpy as np
            import skimage.io

            arr = skimage.io.imread(path, as_gray=True)
            hist, _ = np.histogram(arr, bins=256, range=(0.0, 1.0))
            total = float(hist.sum()) or 1.0
            mean = float(((np.arange(256) + 0.5) / 256.0 * hist).sum() / total) * 255.0
            return mean, float(hist[0:3].sum() / total), float(hist[253:256].sum() / total)
    except Exception:
        return None
    return None


def _render_bands(path, backend, width, height, color):
    """解码 → resize → 转成目标通道顺序。返回 (bands, width, height)。

    bands 是尺寸为 (W,H) 的单通道字节序列列表，顺序即目标通道顺序。
    NCHW 直接拼起来；NHWC 需要交错。
    """
    if backend == "PIL":
        from PIL import Image

        with Image.open(path) as raw:
            im = raw.convert("RGB")
            resample = getattr(Image, "Resampling", Image).BILINEAR
            im = im.resize((width, height), resample)
            if color == "gray":
                return [im.convert("L").tobytes()], width, height
            red, green, blue = im.split()
            if color == "bgr":
                return [blue.tobytes(), green.tobytes(), red.tobytes()], width, height
            return [red.tobytes(), green.tobytes(), blue.tobytes()], width, height

    if backend == "cv2":
        import cv2

        im = cv2.imread(path, cv2.IMREAD_COLOR)  # BGR
        if im is None:
            raise ValueError("读不出图像: %s" % path)
        im = cv2.resize(im, (width, height), interpolation=cv2.INTER_LINEAR)
        if color == "gray":
            gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
            return [gray.tobytes()], width, height
        if color == "rgb":
            im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        planes = [im[:, :, c].tobytes() for c in range(3)]
        return planes, width, height

    if backend == "skimage":
        import numpy as np
        import skimage.io
        import skimage.transform

        arr = skimage.io.imread(path)
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        arr = arr[:, :, :3]
        out = skimage.transform.resize(
            arr, (height, width), preserve_range=True, anti_aliasing=True
        )
        out = np.clip(out, 0, 255)
        u8 = out.astype("uint8")
        if color == "gray":
            gray = (0.299 * u8[:, :, 0] + 0.587 * u8[:, :, 1] + 0.114 * u8[:, :, 2])
            return [gray.astype("uint8").tobytes()], width, height
        if color == "bgr":
            u8 = u8[:, :, ::-1]
        return [u8[:, :, c].tobytes() for c in range(3)], width, height

    raise RuntimeError("没有可用的图像后端")


def _interleave(bands, width, height):
    """把平面数据交错成 NHWC。用 bytearray 切片赋值，避免逐像素 Python 循环。"""
    channels = len(bands)
    out = bytearray(width * height * channels)
    for c, band in enumerate(bands):
        out[c::channels] = band
    return bytes(out)


def _to_dtype(raw, dtype, mean, scale, channels, planar):
    """uint8 原样；float32 需要转换。

    默认**不做归一化**：官方把 norm_type 的均值/缩放插入到模型里，校准数据
    应当保持 input_type_train 格式的原始像素。mean/scale 只在显式给出时才应用。

    `planar` 决定按通道取值时的下标算法：NCHW 同一通道连续，NHWC 通道轮转。
    这个必须由调用方显式告知，不能靠猜。
    """
    if dtype == "uint8":
        if mean is not None or scale is not None:
            raise ValueError("uint8 校准数据不能叠加 mean/scale（值会溢出）。"
                             "需要归一化请用 float32。")
        return raw

    if dtype != "float32":
        raise ValueError("不支持的 cal_data_type: %s" % dtype)

    if mean is None and scale is None:
        values = array.array("f", (float(v) for v in raw))
        return values.tobytes()

    means = list(mean) if mean else [0.0]
    scales = list(scale) if scale else [1.0]
    if len(means) == 1:
        means = means * channels
    if len(scales) == 1:
        scales = scales * channels

    plane = len(raw) // channels
    values = array.array("f")
    append = values.append
    for i, v in enumerate(raw):
        c = (i // plane) if planar else (i % channels)
        append((float(v) - means[c]) * scales[c])
    return values.tobytes()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_env(_argv):
    backend = detect_imaging()
    info = {
        "ok": True,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "imaging_backend": backend,
        "imaging_candidates": [],
        "onnx": None,
        "numpy": None,
    }
    for name, loader in BACKENDS:
        try:
            loader()
            info["imaging_candidates"].append(name)
        except Exception:
            pass
    try:
        import onnx

        info["onnx"] = getattr(onnx, "__version__", "unknown")
    except Exception:
        pass
    try:
        import numpy

        info["numpy"] = getattr(numpy, "__version__", "unknown")
    except Exception:
        pass
    return finish(info)


def cmd_inputs(argv):
    if not argv:
        return fail("用法: probe.py inputs <model.onnx>")
    path = argv[0]
    if not os.path.isfile(path):
        return fail("模型文件不存在: %s" % path)

    size = os.path.getsize(path)
    payload = {
        "ok": True,
        "path": os.path.basename(path),
        "size": size,
        "python": sys.version.split()[0],
        "ir_version": None,
        "opset": None,
        "inputs": [],
        "outputs": [],
        "op_histogram": [],
        "problems": [],
        "warnings": [],
    }

    if size > 2 * 1024 ** 3:
        payload["problems"].append("模型超过 2 GB，超出 protobuf 单文件上限")

    try:
        import onnx
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = "容器里没有 onnx：%s" % exc
        return finish(payload)

    payload["onnx"] = getattr(onnx, "__version__", "unknown")
    try:
        model = onnx.load(path)
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = "onnx 解析失败：%s" % exc
        return finish(payload)

    graph = model.graph
    payload["ir_version"] = int(getattr(model, "ir_version", 0) or 0)
    for opset in getattr(model, "opset_import", []) or []:
        # 取默认域的 opset
        if getattr(opset, "domain", "") in ("", "ai.onnx"):
            payload["opset"] = int(opset.version)
            break

    def describe(value_infos):
        out = []
        for vi in value_infos:
            tensor = vi.type.tensor_type
            dims = []
            dynamic = False
            for dim in tensor.shape.dim:
                if dim.HasField("dim_value") and dim.dim_value > 0:
                    dims.append(int(dim.dim_value))
                else:
                    dims.append(None)
                    dynamic = True
            out.append({
                "name": vi.name,
                "shape": dims,
                "dtype": _onnx_dtype_name(tensor.elem_type),
                "dynamic": dynamic,
            })
        return out

    payload["inputs"] = describe(graph.input)
    payload["outputs"] = describe(graph.output)

    counts = {}
    for node in graph.node:
        counts[node.op_type] = counts.get(node.op_type, 0) + 1
    payload["op_histogram"] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:40]

    # 初版校验规则（细节由宿主侧复用，这里给出原始事实 + 明显问题）
    if len(payload["inputs"]) > 1:
        payload["warnings"].append(
            "输入节点多于一个（%d 个）；官方强烈建议显式写出 input_shape 等参数"
            % len(payload["inputs"]))
    for item in payload["inputs"]:
        if item["dynamic"]:
            payload["problems"].append(
                "输入 %s 含动态维度 %s —— hb_mapper infer 不支持动态 shape，请先固定下来"
                % (item["name"], item["shape"]))
        # 官方要求 ONNX 输入是 float32。实测拿一个 float16 输入的模型跑 checker，
        # 会报一句很难懂的 "unsupported input data type" —— 提前在这里说清楚。
        if item["dtype"] not in ("float32", "unknown(0)"):
            payload["problems"].append(
                "输入 %s 的类型是 %s，官方要求 float32。请在训练侧以 float32 导出后再量化"
                % (item["name"], item["dtype"]))
    return finish(payload)


_DTYPE_NAMES = {
    1: "float32", 2: "uint8", 3: "int8", 4: "uint16", 5: "int16",
    6: "int32", 7: "int64", 9: "bool", 10: "float16", 11: "float64",
    12: "uint32", 13: "uint64",
}


def _onnx_dtype_name(elem_type):
    return _DTYPE_NAMES.get(int(elem_type), "unknown(%d)" % elem_type)


def cmd_calib_scan(argv):
    if not argv:
        return fail("用法: probe.py calib-scan <目录> [--shape WxH] [--dtype float32]")
    directory = argv[0]
    shape, dtype = _parse_scan_opts(argv[1:])

    if not os.path.isdir(directory):
        return fail("目录不存在: %s" % directory)

    backend = detect_imaging()
    entries = []
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        entries.append((name, full, os.path.getsize(full)))

    image_like = [(n, f, s) for n, f, s in entries if n.lower().endswith(IMAGE_EXTS)]
    binary_like = [(n, f, s) for n, f, s in entries
                   if n.lower().endswith(BINARY_EXTS) or "." not in n]

    payload = {
        "ok": True,
        "directory": os.path.basename(os.path.normpath(directory)),
        "imaging_backend": backend,
        "count": len(image_like) + len(binary_like),
        "image_like": len(image_like),
        "binary_like": len(binary_like),
        "measured": bool(backend and image_like),
        "files": [],
        "resolutions": [],
        "duplicates": [],
        "exposure": None,
        "problems": [],
        "warnings": [],
    }

    if not entries:
        payload["problems"].append("目录是空的")
        return finish(payload)

    if backend is None and image_like:
        payload["warnings"].append(
            "容器里没有可用的图像库（PIL / skimage / cv2 都没有），"
            "只做了文件计数，无法核对分辨率与曝光")

    resolution_counts = {}
    digests = {}
    duplicates = []

    for index, (name, full, size) in enumerate(image_like):
        width = height = mode = None
        if backend:
            width, height, mode = image_size(full, backend)
        key = (width, height)
        resolution_counts[key] = resolution_counts.get(key, 0) + 1

        # 内容指纹：大小 + 前 64KB 的 sha1，足够抓重复且很便宜
        try:
            with open(full, "rb") as handle:
                head = handle.read(65536)
            digest = hashlib.sha1(head).hexdigest() + ":%d" % size
        except OSError:
            digest = None
        if digest:
            if digest in digests:
                duplicates.append([digests[digest], name])
            else:
                digests[digest] = name

        if index < MAX_FILE_DETAILS:
            payload["files"].append({
                "path": name, "size": size,
                "width": width, "height": height, "mode": mode,
            })

    payload["resolutions"] = [
        [w, h, c] for (w, h), c in sorted(
            resolution_counts.items(), key=lambda kv: -kv[1])
    ]
    payload["duplicates"] = duplicates

    # ---- 曝光统计 ----
    if backend and image_like:
        samples = image_like[:EXPOSURE_SAMPLE]
        means, blacks, whites = [], [], []
        for _name, full, _size in samples:
            stats = exposure_stats(full, backend)
            if stats is None:
                continue
            mean, near_black, near_white = stats
            means.append(mean)
            blacks.append(near_black)
            whites.append(near_white)
        if means:
            payload["exposure"] = {
                "sampled": len(means),
                "min_mean": round(min(means), 2),
                "max_mean": round(max(means), 2),
                "max_near_black": round(max(blacks), 4),
                "max_near_white": round(max(whites), 4),
            }
            if max(whites) > 0.98:
                payload["problems"].append(
                    "有图片几乎全白（近白像素占比 %.0f%%）——官方要求排除纯白等偏僻场景"
                    % (max(whites) * 100))
            if max(blacks) > 0.98:
                payload["problems"].append(
                    "有图片几乎全黑（近黑像素占比 %.0f%%）——官方要求排除纯黑等偏僻场景"
                    % (max(blacks) * 100))
            if max(means) > 245:
                payload["warnings"].append(
                    "有图片过曝（最高均值 %.1f）" % max(means))
            if min(means) < 10:
                payload["warnings"].append(
                    "有图片欠曝（最低均值 %.1f）" % min(means))

    # ---- 分辨率 ----
    real_resolutions = [r for r in payload["resolutions"] if r[0] and r[1]]
    if len(real_resolutions) > 1:
        # **只列前几个**。实测过一个数据集有 1474 种分辨率，全列出来这条消息上万字符，
        # 会把结论表撑爆。用户需要的是"种类数"和"最典型的几个"，不是完整清单。
        shown = "、".join(
            "%dx%d×%d" % (w, h, c) for w, h, c in real_resolutions[:5])
        more = "，等 %d 种" % len(real_resolutions) if len(real_resolutions) > 5 else ""
        payload["warnings"].append(
            "存在 %d 种分辨率：%s%s" % (len(real_resolutions), shown, more))

    if duplicates:
        payload["warnings"].append("检测到 %d 组内容重复的图片" % len(duplicates))

    # ---- 二进制校准数据的字节数校验 ----
    if binary_like and shape:
        expected = _expected_bytes(shape, dtype)
        bad = []
        for name, _full, size in binary_like:
            if expected and size != expected:
                bad.append("%s(%d≠%d)" % (name, size, expected))
        if bad:
            payload["problems"].append(
                "二进制文件大小与 %s / %s 不匹配：%s"
                % ("x".join(str(d) for d in shape), dtype, "、".join(bad[:5])))
    elif binary_like and not shape:
        payload["warnings"].append(
            "目录里是二进制校准数据；没给 --shape 就无法核对字节数")

    return finish(payload)


def _parse_scan_opts(argv):
    shape, dtype = None, "float32"
    i = 0
    while i < len(argv):
        if argv[i] == "--shape" and i + 1 < len(argv):
            shape = _parse_shape(argv[i + 1])
            i += 2
        elif argv[i] == "--dtype" and i + 1 < len(argv):
            dtype = argv[i + 1]
            i += 2
        else:
            i += 1
    return shape, dtype


def _parse_shape(text):
    """'1x3x640x640' 或 '640x640' → 元组。取最后两维作为 W/H 由调用方决定。

    **全角乘号 `×` 也要认**：界面上显示形状用的是 `×`（更好看），用户也可能自己敲。
    只认 ASCII 的话会静默返回 None —— 实测踩过：`--shape 1×3×640×640` 变成 None，
    字节数校验被悄悄跳过。
    """
    parts = []
    normalized = str(text).lower().replace("×", "x").replace("*", "x")
    for chunk in normalized.replace(",", "x").split("x"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parts.append(int(chunk))
        except ValueError:
            return None
    return tuple(parts) or None


def _expected_bytes(shape, dtype):
    """按 shape 与 dtype 算单个样本应有的字节数。"""
    if not shape:
        return None
    count = 1
    for dim in shape:
        count *= int(dim)
    itemsize = 4 if dtype == "float32" else 1
    return count * itemsize


def sample_evenly(files, count):
    """从有序列表里**均匀**取 `count` 个。

    为什么用间隔取样而不是随机：
      * 确定 —— 同样的输入永远得到同样的子集，用户能看到、也能复现
      * 数据集通常是按时间/场景排好序的，间隔取样天然覆盖各个片段，
        而随机取样可能全挤在某一段

    实测场景：一个 3504 张、1474 种分辨率的装甲板数据集，均匀取 50 张即可代表全局。
    """
    count = int(count or 0)
    if count <= 0 or count >= len(files):
        return list(files)
    step = len(files) / float(count)
    picked = []
    seen = set()
    for i in range(count):
        index = int(i * step)
        if index not in seen:              # 极短列表下可能重复，去一下
            seen.add(index)
            picked.append(files[index])
    return picked


def _image_files(directory):
    """目录下的图片文件名，排序后返回（只取本层，不递归）。"""
    return sorted(
        name for name in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, name))
        and name.lower().endswith(IMAGE_EXTS)
    )


def cmd_calib_build(argv):
    """用法: calib-build <src> <out> --shape WxH --layout NHWC|NCHW
             --color rgb|bgr|gray --dtype uint8|float32
             [--count N] [--mean ..] [--scale ..]

    `--count N` 会先从源目录里**均匀采样** N 张再转换。任何尺寸的图都会被缩放到
    `--shape` 指定的目标尺寸，所以源目录分辨率参差不齐也没关系。

    ⚠️ 产物是**无文件头的裸字节**，长度必须恰好 W*H*C*itemsize。
    实测（见 tests/fixtures/CONTAINER_FINDINGS.md）：`cal_data_dir` 不走 np.load，
    而是把文件当裸字节读、按 cal_data_type 重新解释。放 `.npy` 会失败，报
    "cannot reshape array of size N into shape ..."，N 里多出来的正是 128 字节文件头。
    **不要"优化"成 .npy。**
    """
    if len(argv) < 2:
        return fail("用法: probe.py calib-build <源目录> <输出目录> --shape WxH "
                    "--layout NHWC --color bgr --dtype float32 [--count N]")
    src, out = argv[0], argv[1]
    opts = _parse_build_opts(argv[2:])

    if not os.path.isdir(src):
        return fail("源目录不存在: %s" % src)

    backend = detect_imaging()
    if backend is None:
        return fail("容器里没有可用的图像库（PIL / skimage / cv2），无法生成二进制校准数据")

    shape = opts["shape"]
    if not shape or len(shape) < 2:
        return fail("必须给出 --shape，例如 --shape 640x640")
    width, height = int(shape[-1]), int(shape[-2])
    channels = 1 if opts["color"] == "gray" else 3
    layout = opts["layout"]
    dtype = opts["dtype"]

    files = _image_files(src)
    if not files:
        return fail("源目录里没有可识别的图片")

    available = len(files)
    files = sample_evenly(files, opts["count"])

    if not os.path.isdir(out):
        os.makedirs(out)

    written = 0
    total_bytes = 0
    failures = []
    total = len(files)
    # 每个样本应有的字节数：裸字节，没有文件头。生成完逐个自检，
    # 因为字节数不对时工具只会报一句难懂的 reshape 错误。
    per_sample = _expected_bytes((1, channels, height, width), dtype)
    mismatched = []
    progress(0, total)

    for index, name in enumerate(files, start=1):
        source_path = os.path.join(src, name)
        try:
            bands, w, h = _render_bands(
                source_path, backend, width, height, opts["color"])
            if layout == "NCHW":
                raw = b"".join(bands)
                planar = True
            else:
                raw = _interleave(bands, w, h)
                planar = False
            data = _to_dtype(raw, dtype, opts["mean"], opts["scale"],
                             channels, planar)

            target = os.path.join(out, os.path.splitext(name)[0] + ".bin")
            with open(target, "wb") as handle:
                handle.write(data)
            written += 1
            total_bytes += len(data)
            if per_sample is not None and len(data) != per_sample:
                mismatched.append("%s(%d≠%d)" % (name, len(data), per_sample))
        except Exception as exc:
            failures.append("%s: %s" % (name, exc))
        progress(index, total)

    payload = {
        "ok": written > 0,
        "backend": backend,
        "written": written,
        "failed": len(failures),
        "failures": failures[:10],
        "bytes": total_bytes,
        "bytes_per_sample": per_sample,
        "out_dir": os.path.basename(os.path.normpath(out)),
        "shape": list(shape),
        "layout": layout,
        "color": opts["color"],
        "dtype": dtype,
        "normalized": bool(opts["mean"] or opts["scale"]),
        # 采样信息：让界面能说清"从 3504 张里取了 50 张"
        "available": available,
        "sampled": len(files),
    }
    if mismatched:
        payload["ok"] = False
        payload["error"] = ("产出的字节数不符合预期（工具只接受无文件头的裸字节，"
                            "长度必须恰好等于 形状×类型大小）：" + "、".join(mismatched[:5]))
    elif written == 0:
        payload["error"] = "没有一张图转换成功"
    return finish(payload)


def _parse_build_opts(argv):
    opts = {
        "shape": None, "layout": "NHWC", "color": "bgr",
        "dtype": "float32", "mean": None, "scale": None, "count": 0,
    }
    i = 0
    while i < len(argv):
        key = argv[i]
        value = argv[i + 1] if i + 1 < len(argv) else None
        if key == "--shape":
            opts["shape"] = _parse_shape(value)
            i += 2
        elif key == "--count":
            try:
                opts["count"] = int(value)
            except (TypeError, ValueError):
                opts["count"] = 0
            i += 2
        elif key == "--layout":
            opts["layout"] = (value or "NHWC").upper()
            i += 2
        elif key == "--color":
            opts["color"] = (value or "bgr").lower()
            i += 2
        elif key == "--dtype":
            opts["dtype"] = (value or "float32").lower()
            i += 2
        elif key == "--mean":
            opts["mean"] = _parse_floats(value)
            i += 2
        elif key == "--scale":
            opts["scale"] = _parse_floats(value)
            i += 2
        else:
            i += 1
    return opts


def _parse_floats(text):
    if not text:
        return None
    parts = []
    for chunk in str(text).replace(",", " ").split():
        try:
            parts.append(float(chunk))
        except ValueError:
            continue
    return parts or None


def cmd_thumbs(argv):
    """用法: probe.py thumbs <源目录> <输出目录> [--size 96] [--count N]

    `--count N` 与 `calib-build` 用**同一套均匀采样**，所以看到的缩略图就是
    实际会用来生成校准数据的那几张。
    """
    if len(argv) < 2:
        return fail("用法: probe.py thumbs <源目录> <输出目录> [--size 96] [--count N]")
    src, out = argv[0], argv[1]
    size = 96
    count = 0
    for flag, cast in (("--size", int), ("--count", int)):
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                try:
                    value = cast(argv[idx + 1])
                except (TypeError, ValueError):
                    continue
                if flag == "--size":
                    size = value
                else:
                    count = value

    backend = detect_imaging()
    if backend is None:
        return fail("容器里没有可用的图像库，无法生成缩略图")

    if not os.path.isdir(out):
        os.makedirs(out)

    made = []
    failures = []
    # 和 calib-build 同一套采样 —— 看到的缩略图就是实际会用的那几张
    names = sample_evenly(_image_files(src), count)
    if backend == "PIL":
        from PIL import Image

        for name in names:
            full = os.path.join(src, name)
            if not os.path.isfile(full):
                continue
            try:
                with Image.open(full) as im:
                    im = im.convert("RGB")
                    resample = getattr(Image, "Resampling", Image).BILINEAR
                    im.thumbnail((size, size), resample)
                    target = os.path.join(out, os.path.splitext(name)[0] + ".png")
                    im.save(target, "PNG")
                made.append(os.path.basename(target))
            except Exception as exc:
                failures.append("%s: %s" % (name, exc))
    else:
        import numpy as np

        try:
            import cv2
        except Exception:
            cv2 = None
        try:
            import skimage.io
            import skimage.transform
        except Exception:
            skimage = None

        for name in names:
            full = os.path.join(src, name)
            if not os.path.isfile(full):
                continue
            try:
                target = os.path.join(out, os.path.splitext(name)[0] + ".png")
                if cv2 is not None:
                    im = cv2.imread(full, cv2.IMREAD_COLOR)
                    if im is None:
                        raise ValueError("读不出图像")
                    h, w = im.shape[:2]
                    scale = float(size) / max(h, w)
                    if scale < 1.0:
                        im = cv2.resize(im, (max(1, int(w * scale)),
                                             max(1, int(h * scale))))
                    cv2.imwrite(target, im)
                else:
                    arr = skimage.io.imread(full)
                    arr = skimage.transform.resize(
                        arr, (size, size), preserve_range=True).astype("uint8")
                    skimage.io.imsave(target, arr)
                made.append(os.path.basename(target))
            except Exception as exc:
                failures.append("%s: %s" % (name, exc))

    return finish({
        "ok": bool(made),
        "backend": backend,
        "generated": len(made),
        "failed": len(failures),
        "files": made[:400],
    })


COMMANDS = {
    "env": cmd_env,
    "inputs": cmd_inputs,
    "calib-scan": cmd_calib_scan,
    "calib-build": cmd_calib_build,
    "thumbs": cmd_thumbs,
}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        return finish({"ok": True, "commands": sorted(COMMANDS)})
    handler = COMMANDS.get(argv[0])
    if handler is None:
        return fail("未知子命令: %s（可用：%s）"
                    % (argv[0], "、".join(sorted(COMMANDS))))
    try:
        return handler(argv[1:])
    except Exception as exc:
        return fail("%s: %s" % (type(exc).__name__, exc), traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
