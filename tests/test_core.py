# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""core 层单元测试。

重点覆盖两处最难端到端验证的逻辑：
  1) `build_run_argv` —— 本机没有 Docker，但 argv 拼装是最容易出错的地方；
  2) 断点续传 —— 10 GB 的镜像在国内网络下必断，续传逻辑必须真的对。
"""

from __future__ import annotations

import http.server
import os
import threading
from pathlib import Path

import pytest

from rdkquant.core import images as img
from rdkquant.core.backends import MockBackend, Workspace, make_backend
from rdkquant.core.probe import LEVEL_ERROR, LEVEL_WARN, probe_host, recommended_shm_bytes
from rdkquant.core.runner import (
    Mount,
    RunSpec,
    build_run_argv,
    default_shm_size,
    normalize_host_path,
)

GB = 1024 ** 3


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------
def test_manifest_covers_x3_and_x5():
    specs = {s.id: s for s in img.load_manifest()}
    assert "x5-1.2.8-cpu" in specs
    assert "x3-2.6.6-cpu" in specs
    assert specs["x5-1.2.8-cpu"].march == "bayes-e"
    assert specs["x3-2.6.6-cpu"].march == "bernoulli2"
    for spec in specs.values():
        assert spec.url.startswith("https://d-robotics-aitoolchain.oss-cn-beijing.aliyuncs.com/")
        assert spec.url.endswith(".tar.gz")


def test_get_spec_unknown_id():
    with pytest.raises(KeyError):
        img.get_spec("does-not-exist")


def test_tar_name_extracted_from_url():
    spec = img.get_spec("x5-1.2.8-cpu")
    assert spec.tar_name == "docker_openexplorer_ubuntu_20_x5_cpu_v1.2.8.tar.gz"


# --------------------------------------------------------------------------
# argv 拼装
# --------------------------------------------------------------------------
def _spec(**kwargs) -> RunSpec:
    base = dict(
        image="openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8",
        workspace=Path("/data/run1"),
        command=["hb_mapper", "makertbin", "--config", "/workspace/config.yaml"],
        shm_size="4g",
        windows=False,
    )
    base.update(kwargs)
    return RunSpec(**base)


def test_run_argv_shape():
    argv = build_run_argv(_spec())
    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    assert argv[argv.index("--shm-size") + 1] == "4g"
    assert argv[argv.index("-v") + 1] == "/data/run1:/workspace"
    # 镜像必须在命令之前
    image_idx = argv.index("openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8")
    assert argv[image_idx + 1 :] == ["hb_mapper", "makertbin", "--config", "/workspace/config.yaml"]


def test_run_argv_windows_normalizes_backslashes_and_skips_user():
    argv = build_run_argv(
        _spec(workspace=Path(r"C:\Users\ccron\Desktop\run1"), windows=True)
    )
    mount = argv[argv.index("-v") + 1]
    assert "\\" not in mount
    assert mount.endswith(":/workspace")
    # Windows 上没有 uid/gid，传 --user 会直接报错
    assert "--user" not in argv


def test_ownership_fix_is_skipped_when_everything_is_ours(tmp_path: Path):
    """工作区里的文件都属于当前用户时，不该为此多起一个容器。

    stat 是便宜的，但多起一次容器不是（约 1 秒）。所以先查再动手。
    """
    from rdkquant.core.runner import ContainerRunner

    runner = ContainerRunner(image="img:tag", workspace=tmp_path)
    (tmp_path / "mine.txt").write_text("x")
    assert runner._ownership_needs_fixing() is False


def test_run_argv_never_passes_user_by_default():
    """默认**不能**传 `--user` —— 传了工具链整个跑不起来。

    这里曾经是反过来的：早先传 `--user $(id -u):$(id -g)`，理由是"别让挂载卷里的
    产物变成 root 所有"。那个理由被实测推翻了：X5 CPU 镜像把 torch 装在
    `/root/.local/lib/python3.10/site-packages/`，而 `/root` 权限是 700 —— uid 1000
    连进都进不去，`hmct/ir/onnx_graph.py` 那句 `import torch` 直接 ModuleNotFoundError，
    `hb_mapper` 连 `--version` 都打不出来。

    产物属主改用"跑完之后按需 chown"解决，不拿工具链的可用性去换。
    """
    argv = build_run_argv(_spec(windows=False))
    assert "--user" not in argv


def test_run_argv_can_still_force_a_user_if_asked():
    """留一个显式出口，但默认关着。"""
    argv = build_run_argv(_spec(windows=False, run_as_root=False, user="1000:1000"))
    assert argv[argv.index("--user") + 1] == "1000:1000"


@pytest.mark.skipif(os.name == "nt", reason="Windows 上没有 os.getuid()")
def test_run_argv_derives_user_from_process_when_asked():
    argv = build_run_argv(_spec(windows=False, run_as_root=False))
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_run_argv_gpus_and_readonly_mounts():
    argv = build_run_argv(
        _spec(
            gpus=True,
            extra_mounts=(Mount(Path("/data/cal"), "/cal", read_only=True),),
        )
    )
    assert argv[argv.index("--gpus") + 1] == "all"
    assert "/data/cal:/cal:ro" in argv


def test_run_argv_env_pairs():
    argv = build_run_argv(_spec(env=(("HF_ENDPOINT", "https://mirror"),)))
    assert argv[argv.index("-e") + 1] == "HF_ENDPOINT=https://mirror"


def test_normalize_host_path_posix_target_is_untouched_on_windows():
    # 在 Windows 上模拟 Linux 目标时，绝不能把 /opt/data 解析成 C:\opt\data
    assert normalize_host_path("/opt/data", windows=False) == "/opt/data"


def test_human_bytes():
    assert img.human_bytes(0) == "0 B"
    assert img.human_bytes(None) == "0 B"
    assert img.human_bytes(512) == "512 B"
    assert img.human_bytes(1536) == "1.5 KB"
    assert img.human_bytes(2 * GB) == "2.00 GB"


# --------------------------------------------------------------------------
# shm 尺寸：官方示例的 15g 在 16 GB 机器上会拖死系统
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "total,expected",
    [
        (0, "4g"),
        (4 * GB, "1g"),
        (16 * GB, "4g"),
        (32 * GB, "8g"),
        (128 * GB, "8g"),   # 上限 8g
        (2 * GB, "1g"),     # 下限 1g
    ],
)
def test_default_shm_size_clamping(total, expected):
    assert default_shm_size(total) == expected
    assert recommended_shm_bytes(total) == int(expected[:-1]) * GB


# --------------------------------------------------------------------------
# 工作区
# --------------------------------------------------------------------------
def test_workspace_layout_and_rel(tmp_path: Path):
    ws = Workspace(tmp_path / "run1").ensure()
    for d in (ws.model_dir, ws.calib_dir, ws.output_dir, ws.log_dir):
        assert d.is_dir()

    inside = ws.calib_dir / "a.jpg"
    assert ws.rel(inside) == "/workspace/cal_images/a.jpg"

    outside = tmp_path / "elsewhere" / "b.onnx"
    # 不在工作区内的路径原样返回，交由上层拒绝——绝不静默拼一个错误路径
    assert not ws.rel(outside).startswith("/workspace/")


# --------------------------------------------------------------------------
# 断点续传（真的起一个支持 Range 的 HTTP 服务）
# --------------------------------------------------------------------------
class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload = b""

    def _send(self, body: bytes, status: int, extra: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self._send(self.payload, 200, {})

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start = int(rng[len("bytes=") :].split("-")[0])
            body = self.payload[start:]
            self._send(
                body,
                206,
                {"Content-Range": f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}"},
            )
        else:
            self._send(self.payload, 200, {})

    def log_message(self, *args):  # 静音
        pass


@pytest.fixture()
def range_server():
    _RangeHandler.payload = bytes(range(256)) * 64  # 16 KB，够切两段
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/img.tar.gz", _RangeHandler.payload
    server.shutdown()
    server.server_close()


def test_download_fresh(range_server, tmp_path: Path):
    url, payload = range_server
    dest = tmp_path / "img.tar.gz"
    seen: list[tuple[int, int]] = []
    result = img.download(url, dest, on_progress=lambda d, t: seen.append((d, t)))

    assert dest.read_bytes() == payload
    assert result.resumed_from == 0
    assert result.downloaded_bytes == len(payload)
    assert seen[-1][0] == len(payload)
    assert not (tmp_path / "img.tar.gz.part").exists(), "成功后必须清掉 .part"


def test_download_resumes_from_partial_file(range_server, tmp_path: Path):
    url, payload = range_server
    dest = tmp_path / "img.tar.gz"
    part = tmp_path / "img.tar.gz.part"

    # 模拟上次下到一半就断了
    cut = 4096
    part.write_bytes(payload[:cut])

    lines: list[str] = []
    result = img.download(url, dest, resume=True, on_line=lines.append)

    assert dest.read_bytes() == payload, "续传后必须是完整的原始内容"
    assert result.resumed_from == cut
    assert any("续传" in line for line in lines)


def test_download_cancel_leaves_part_for_resume(range_server, tmp_path: Path):
    url, payload = range_server
    dest = tmp_path / "img.tar.gz"
    cancel = threading.Event()
    cancel.set()  # 一开始就取消

    with pytest.raises(img.Cancelled):
        img.download(url, dest, cancel=cancel, max_retries=1)
    assert not dest.exists()


def test_probe_remote_reports_length_and_range_support(range_server):
    url, payload = range_server
    info = img.probe_remote(url)
    assert info.ok
    assert info.content_length == len(payload)
    assert info.accept_ranges


# --------------------------------------------------------------------------
# 镜像名解析：不要硬编码
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hint,candidates,expected",
    [
        ("a/b:1", ["a/b:1"], "a/b:1"),
        ("a/b:1", ["a/b:2"], "a/b:2"),          # tag 不同也能认出来
        ("a/b:1", ["x/y:1"], None),
        ("a/b", ["a/b:9"], "a/b:9"),
    ],
)
def test_find_loaded(hint, candidates, expected):
    assert img.find_loaded(hint, candidates) == expected


def test_loaded_image_regex():
    assert img._LOADED_RE.search("Loaded image: openexplorer/x5_cpu:v1.2.8").group(1) == (
        "openexplorer/x5_cpu:v1.2.8"
    )
    assert img._LOADED_RE.search("Loaded image ID: sha256:abc").group(1) == "sha256:abc"


def test_ensure_image_short_circuits_when_present(monkeypatch, tmp_path: Path):
    spec = img.get_spec("x5-1.2.8-cpu")
    monkeypatch.setattr(img, "find_loaded", lambda hint, pool=None: spec.image_ref_hint)
    lines: list[str] = []
    result = img.ensure_image(spec, cache_dir=tmp_path, on_line=lines.append)
    assert result.action == "already-present"
    assert result.image_ref == spec.image_ref_hint


# --------------------------------------------------------------------------
# 体检 + 后端
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("24.0.7", (24, 0, 7)),
        ("Docker version 20.10.24, build 5d6db84", (20, 10, 24)),
        ("19.03", (19, 3)),
        ("", None),
        (None, None),
        ("no digits here", None),
    ],
)
def test_parse_version(text, expected):
    from rdkquant.core.probe import parse_version

    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "version,target,expected",
    [
        ((24, 0, 7), (19, 3), False),
        ((19, 2), (19, 3), True),
        ((19, 3), (19, 3), False),
        ((20,), (19, 3), False),
        # 关键用例：长度不同时按"补零"比较，(19,) 不该被判成小于 (19, 3)
        ((19,), (19, 3), True),
        (None, (19, 3), False),   # 版本未知时不误报
    ],
)
def test_version_lt(version, target, expected):
    from rdkquant.core.probe import version_lt

    assert version_lt(version, target) is expected


def test_probe_host_never_raises_and_reports_something(tmp_path: Path, monkeypatch):
    """体检必须总能跑完并给出结论——**不依赖这台机器装没装 Docker、磁盘够不够**。

    原来这条直接断言"没装 Docker 就应当给出 FAIL 或 can_quantize"，把测试绑在了
    开发机的真实状态上。在用户的 Windows 机器上就不成立了：Docker Desktop 装着
    （所以不是 MISSING）、磁盘只剩 3.4 GB（所以 can_quantize 是 False）——
    两个条件都不满足就挂。而 Docker 守护进程偶尔响应慢时又会通过，于是表现为
    **环境相关的偶发失败**。测试不该有这种脾气。
    """
    import shutil

    from rdkquant.core.probe import GB

    # 把磁盘固定成充足，去掉环境里最容易变的那一项
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: shutil._ntuple_diskusage(1000 * GB, 500 * GB, 500 * GB),
    )
    report = probe_host(tmp_path)
    assert report.platform
    assert report.findings, "体检至少要给出结论"
    assert isinstance(report.can_quantize, bool)
    assert isinstance(report.to_dict(), dict)


def test_probe_host_flags_disk_shortage(tmp_path: Path):
    huge = 10_000 * GB
    report = probe_host(tmp_path, required_image_bytes=huge, required_image_name="假镜像")
    assert any(f.code == "DISK_INSUFFICIENT" and f.level == LEVEL_ERROR for f in report.findings)
    assert not report.can_quantize


def _stub_docker(monkeypatch, version: str, *, gpu: bool = False):
    """把 docker / gpu 探测打桩，用来验证版本下限这一层的判断。"""
    from rdkquant.core import probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "probe_docker",
        lambda: probe_mod.DockerStatus("docker", version, True, version),
    )
    devices = ("NVIDIA GeForce RTX 4090, 24564 MiB",) if gpu else ()
    monkeypatch.setattr(
        probe_mod, "probe_gpu", lambda: probe_mod.GpuStatus("nvidia-smi" if gpu else None, devices)
    )
    return probe_mod


def test_probe_host_accepts_current_docker(tmp_path, monkeypatch):
    """docker.io（发行版打包）和 docker-ce（官方）都长这样，都该通过。"""
    probe_mod = _stub_docker(monkeypatch, "24.0.7")
    report = probe_mod.probe_host(tmp_path)
    codes = {f.code for f in report.findings}
    assert "DOCKER_READY" in codes
    assert "DOCKER_TOO_OLD" not in codes


def test_probe_host_rejects_docker_without_workdir_support(tmp_path, monkeypatch):
    """--workdir 需要 Docker 17.09+；更旧的版本直接判错，而不是等到跑容器才炸。"""
    probe_mod = _stub_docker(monkeypatch, "17.6.0")
    report = probe_mod.probe_host(tmp_path)
    assert any(f.code == "DOCKER_TOO_OLD" and f.level == LEVEL_ERROR for f in report.findings)
    assert not report.can_quantize


def test_probe_host_warns_when_docker_too_old_for_gpus(tmp_path, monkeypatch):
    """19.03 之前没有 --gpus：有显卡也用不了 GPU 版镜像，得说清楚。"""
    probe_mod = _stub_docker(monkeypatch, "19.2.0", gpu=True)
    report = probe_mod.probe_host(tmp_path)
    assert any(f.code == "DOCKER_OLD_FOR_GPU" and f.level == LEVEL_WARN for f in report.findings)


def test_no_gpu_warning_when_no_gpu_present(tmp_path, monkeypatch):
    """没显卡就别提 --gpus 了，否则是纯粹的噪音。"""
    probe_mod = _stub_docker(monkeypatch, "19.2.0", gpu=False)
    report = probe_mod.probe_host(tmp_path)
    assert not any(f.code == "DOCKER_OLD_FOR_GPU" for f in report.findings)


def test_mock_backend_end_to_end(tmp_path: Path):
    ws = Workspace(tmp_path / "run").ensure()
    (ws.calib_dir / "1.jpg").write_bytes(b"x")
    backend = MockBackend(workspace=ws, delay=0.0)

    lines: list[str] = []
    assert backend.check(ws.model_dir / "m.onnx", "bayes-e", on_line=lines.append) == 0
    text = "\n".join(lines)
    # 假输出刻意模仿真实格式，所以要按真实格式断言（见 backends._MOCK_CHECK）
    assert "The main quantized node information:" in text
    assert "bayes-e" in text
    # 而且要能被解析器吃下来——否则 Mock 路径测不到解析器的容错逻辑
    from rdkquant.core.logparse import parse_checker

    report = parse_checker(lines)
    assert report is not None
    assert [n.name for n in report.nodes] == [
        "Conv_0", "Conv_2", "Softmax_91", "Resize_118",
    ]

    lines.clear()
    assert backend.compile(on_line=lines.append) == 0
    produced = list(ws.output_dir.glob("*.bin"))
    assert produced, "假后端也要产出产物，否则 UI 的『打开产物』路径测不到"


def test_mock_backend_honours_cancel(tmp_path: Path):
    ws = Workspace(tmp_path / "run").ensure()
    cancel = threading.Event()
    backend = MockBackend(workspace=ws, delay=0.0)
    cancel.set()
    assert backend.compile(on_line=lambda _l: None, cancel=cancel) == 130


def test_make_backend_falls_back_to_mock_without_docker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("rdkquant.core.backends.which", lambda _exe: None)
    backend = make_backend(workspace=Workspace(tmp_path / "run"), image="whatever:1")
    assert backend.name == "mock"
