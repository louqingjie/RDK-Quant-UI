# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""镜像获取。

⚠️ 关键事实（已实测核实，2026）：

    RDK 的 OpenExplorer 工具链镜像**不是从 registry 拉的**，官方提供的是离线
    tar.gz，正确动作是 `docker load -i <tar.gz>`，不是 `docker pull`。

    本机实测 `registry-1.docker.io` 直接连不通，所以离线包是唯一可靠路径。
    因此这个模块把「拉取镜像」拆成三个可独立失败、可分别重试的步骤：
        1) 下载官方 tar.gz（支持 HTTP Range 断点续传 + 自动重试）
        2) docker load 导入（流式回显进度）
        3) 从 docker load 的输出里解析出**真实**镜像名

    第 3 步很重要：镜像名不要硬编码。官方文件名就有 `docker_openexplorer_...`
    和 `docker_open_explorer_...` 两种写法，猜名字必踩坑。
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .proc import Cancelled, LineCallback, run_capture, stream_lines, which

GB = 1024 ** 3
MB = 1024 ** 2

USER_AGENT = "rdkquant/0.1 (+model quantization tool)"

ProgressCallback = Callable[[int, int], None]  # (downloaded, total)


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ImageSpec:
    id: str
    display_name: str
    board: str
    march: str
    variant: str
    oe_version: str
    python: str
    url: str
    image_ref_hint: str
    size_hint_bytes: int | None = None
    note: str = ""

    @property
    def is_gpu(self) -> bool:
        return self.variant == "gpu"

    @property
    def tar_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @classmethod
    def from_dict(cls, d: dict) -> "ImageSpec":
        return cls(
            id=d["id"],
            display_name=d["display_name"],
            board=d["board"],
            march=d["march"],
            variant=d["variant"],
            oe_version=d["oe_version"],
            python=d.get("python", ""),
            url=d["url"],
            image_ref_hint=d.get("image_ref_hint", ""),
            size_hint_bytes=d.get("size_hint_bytes"),
            note=d.get("note", ""),
        )


def load_manifest() -> list[ImageSpec]:
    path = importlib.resources.files("rdkquant").joinpath("data").joinpath("images.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [ImageSpec.from_dict(d) for d in data["images"]]


def get_spec(image_id: str) -> ImageSpec:
    for spec in load_manifest():
        if spec.id == image_id:
            return spec
    raise KeyError(f"清单里没有镜像: {image_id}")


# --------------------------------------------------------------------------
# 第 1 步：下载（可续传 + 重试）
# --------------------------------------------------------------------------
@dataclass
class DownloadResult:
    path: Path
    total_bytes: int
    downloaded_bytes: int
    resumed_from: int = 0


@dataclass
class RemoteInfo:
    content_length: int | None
    accept_ranges: bool
    ok: bool
    error: str = ""


def probe_remote(url: str, *, timeout: float = 30.0) -> RemoteInfo:
    """HEAD 请求：拿到精确体积，并确认服务器是否支持断点续传。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return RemoteInfo(
                content_length=int(length) if length and length.isdigit() else None,
                accept_ranges=(resp.headers.get("Accept-Ranges", "").lower() == "bytes"),
                ok=True,
            )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return RemoteInfo(None, False, False, str(exc))


def download(
    url: str,
    dest: os.PathLike | str,
    *,
    resume: bool = True,
    on_progress: ProgressCallback | None = None,
    on_line: LineCallback | None = None,
    cancel: threading.Event | None = None,
    chunk: int = 1 << 20,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> DownloadResult:
    """下载到 dest。先写 dest.part，成功后再原子改名——中断不会留下半个成品。

    10 GB 的 GPU 镜像在国内网络下经常会断，所以续传不是可选项而是必需项。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    def log(msg: str) -> None:
        if on_line:
            on_line(msg)

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            return _download_once(
                url,
                dest,
                part,
                resume=resume,
                on_progress=on_progress,
                log=log,
                cancel=cancel,
                chunk=chunk,
                timeout=timeout,
            )
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络异常类型太杂，统一重试
            last_error = exc
            if attempt >= max_retries:
                break
            wait = min(30, 2 ** attempt)
            log(f"[重试 {attempt}/{max_retries - 1}] {exc} —— {wait}s 后继续（已下载部分会保留）")
            for _ in range(wait):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                time.sleep(1)
    raise RuntimeError(f"下载失败（已重试 {max_retries} 次）: {last_error}")


def _download_once(
    url: str,
    dest: Path,
    part: Path,
    *,
    resume: bool,
    on_progress: ProgressCallback | None,
    log: Callable[[str], None],
    cancel: threading.Event | None,
    chunk: int,
    timeout: float,
) -> DownloadResult:
    offset = part.stat().st_size if (resume and part.exists()) else 0

    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if offset and status != 206:
            log("[续传不可用] 服务器忽略了 Range 请求，从头开始下载")
            offset = 0
        elif offset:
            log(f"[续传] 从 {human_bytes(offset)} 继续")

        remaining = int(resp.headers.get("Content-Length") or 0)
        total = remaining + offset
        mode = "ab" if offset else "wb"
        done = offset
        if on_progress:
            on_progress(done, total)

        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                if on_progress:
                    on_progress(done, total)

    if total and done < total:
        raise IOError(f"连接提前中断：已收 {done}/{total} 字节")

    os.replace(part, dest)
    log(f"[完成] {dest.name}  {human_bytes(done)}")
    return DownloadResult(dest, total or done, done, offset)


# --------------------------------------------------------------------------
# 第 2 步 / 第 3 步：导入并解析真实镜像名
# --------------------------------------------------------------------------
_LOADED_RE = re.compile(r"Loaded image(?: ID)?:\s*(\S+)")
_LOADING_RE = re.compile(r"^Loaded image", re.IGNORECASE)


def docker_load(
    tar_path: os.PathLike | str,
    *,
    on_line: LineCallback | None = None,
    cancel: threading.Event | None = None,
) -> list[str]:
    """`docker load -i <tar>`，返回实际导入的镜像引用列表。

    这是整个流程里最慢的一步（要把上百层解压写盘），所以必须流式回显，
    否则用户会以为程序卡死了。
    """
    exe = _require_docker()
    tar_path = str(tar_path)
    lines: list[str] = []
    loaded: list[str] = []

    def handle(line: str) -> None:
        lines.append(line)
        m = _LOADED_RE.search(line)
        if m:
            loaded.append(m.group(1))
        if on_line:
            on_line(line)

    rc = stream_lines([exe, "load", "-i", tar_path], handle, cancel=cancel)
    if rc != 0:
        raise RuntimeError(f"docker load 失败（退出码 {rc}）：\n" + "\n".join(lines[-15:]))
    if not loaded:
        # 老版本 docker 可能不输出 "Loaded image:"，回退到按 sha 查
        raise RuntimeError(
            "docker load 成功，但无法从输出中解析出镜像名。"
            "请手动执行 `docker images` 确认，或改用 docker_pull()。"
        )
    return loaded


def docker_pull(
    image_ref: str,
    *,
    on_line: LineCallback | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """从 registry 拉取。RDK 官方镜像不建议走这条路（实测 registry 常连不通），
    但保留给内网自建 registry 或镜像加速器的场景。"""
    exe = _require_docker()
    lines: list[str] = []

    def handle(line: str) -> None:
        lines.append(line)
        if on_line:
            on_line(line)

    rc = stream_lines([exe, "pull", image_ref], handle, cancel=cancel)
    if rc != 0:
        raise RuntimeError(f"docker pull 失败（退出码 {rc}）：\n" + "\n".join(lines[-15:]))


def local_images() -> list[str]:
    """本机已有镜像，格式 repository:tag。"""
    exe = which("docker")
    if not exe:
        return []
    res = run_capture(
        [exe, "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30
    )
    if not res.ok:
        return []
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


def has_image(ref: str) -> bool:
    ref = ref.strip()
    for item in local_images():
        if item == ref:
            return True
        # 允许省略 tag 的比较
        if ":" not in ref and item.split(":")[0] == ref:
            return True
    return False


def find_loaded(ref_hint: str, candidates: Iterable[str] | None = None) -> str | None:
    """在已有镜像里找 hint 对应的那个（容忍 tag 差异）。"""
    pool = list(candidates) if candidates is not None else local_images()
    base = ref_hint.split(":")[0]
    for item in pool:
        if item == ref_hint:
            return item
    for item in pool:
        if item.split(":")[0] == base:
            return item
    return None


# --------------------------------------------------------------------------
# 高层编排：一键「确保镜像可用」
# --------------------------------------------------------------------------
@dataclass
class EnsureResult:
    spec_id: str
    image_ref: str
    action: str  # "already-present" | "loaded" | "pulled" | "downloaded+loaded"
    tar_path: Path | None = None


def ensure_image(
    spec: ImageSpec,
    *,
    cache_dir: os.PathLike | str,
    on_line: LineCallback | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    prefer_pull: bool = False,
    keep_tar: bool = True,
) -> EnsureResult:
    """确保 spec 对应的镜像在本机可用。顺序：本地已有 → 缓存里的 tar → 下载 → load。"""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    tar_path = cache / spec.tar_name

    def log(msg: str) -> None:
        if on_line:
            on_line(msg)

    # 1) 已经装过了？
    existing = find_loaded(spec.image_ref_hint)
    if existing:
        log(f"[跳过] 本机已有镜像 {existing}")
        return EnsureResult(spec.id, existing, "already-present")

    # 2) registry 优先（如果用户明确要求）
    if prefer_pull:
        try:
            log(f"[拉取] docker pull {spec.image_ref_hint}")
            docker_pull(spec.image_ref_hint, on_line=on_line, cancel=cancel)
            return EnsureResult(spec.id, spec.image_ref_hint, "pulled")
        except (RuntimeError, Cancelled):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            log("[回退] registry 拉取失败，改用官方离线包")

    # 3) 下载（或复用缓存里的 tar）
    if tar_path.exists() and tar_path.stat().st_size > 0:
        log(f"[复用] 缓存中已有 {tar_path.name}（{human_bytes(tar_path.stat().st_size)}）")
    else:
        info = probe_remote(spec.url)
        total_txt = human_bytes(info.content_length) if info.content_length else "未知大小"
        log(f"[下载] {spec.url}")
        log(f"       体积 {total_txt}，支持续传={info.accept_ranges}")
        download(
            spec.url,
            tar_path,
            resume=True,
            on_progress=on_progress,
            on_line=on_line,
            cancel=cancel,
        )

    # 4) 导入
    log(f"[导入] docker load -i {tar_path.name}（这一步最慢，请耐心等待）")
    loaded = docker_load(tar_path, on_line=on_line, cancel=cancel)
    if not keep_tar:
        log(f"[清理] 删除离线包 {tar_path.name}")
        tar_path.unlink(missing_ok=True)
    log(f"[就绪] 镜像 {loaded[0]}")
    return EnsureResult(spec.id, loaded[0], "downloaded+loaded", tar_path)


# --------------------------------------------------------------------------
def default_cache_dir() -> Path:
    """离线包缓存目录。

    刻意放在用户级目录而不是项目目录：一个 1.7~10 GB 的离线包应该在多台机器、
    多个项目之间复用，放进项目里会被误删或被 git 收进去。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "rdkquant" / "images"
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "rdkquant" / "images"


def human_bytes(n: int | None) -> str:
    """给人看的体积。KB 保留 1 位小数，MB 以上保留 2 位——1.5 KB 显示成
    「2 KB」会让人以为文件比实际大一倍。"""
    if not n:
        return "0 B"
    for unit, div in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.2f} {unit}" if unit != "KB" else f"{n / div:.1f} {unit}"
    return f"{n} B"


def _require_docker() -> str:
    exe = which("docker")
    if not exe:
        raise RuntimeError("未找到 docker 命令，无法操作镜像。请先安装 Docker。")
    return exe
