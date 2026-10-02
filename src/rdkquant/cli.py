# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""命令行入口。

即使最终产品是图形界面，CLI 也必须存在：
  * CI / 自动化里没法点按钮；
  * 出问题时，让用户跑 `rdkquant doctor` 贴输出，比让他描述界面截图高效得多。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import images as img
from .core.backends import MockBackend, Workspace
from .core.probe import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, GB, probe_host

_ICON = {LEVEL_OK: "[ OK ]", LEVEL_WARN: "[WARN]", LEVEL_ERROR: "[FAIL]"}

#: 不带参数启动时的默认工作区
DEFAULT_WORKSPACE = "./runs/current"


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _render_report(report) -> str:
    out: list[str] = []
    out.append("")
    out.append("RDK 模型量化工具 · 环境体检")
    out.append("=" * 62)
    out.append(f"  平台        : {report.platform}  ({report.arch})")
    out.append(f"  Python      : {report.python}")

    d = report.docker
    if d.cli_path is None:
        out.append("  Docker      : 未安装")
    elif d.daemon_ok:
        out.append(f"  Docker      : 就绪  客户端 {d.cli_version} / 服务端 {d.daemon_version}")
    else:
        out.append(f"  Docker      : 客户端 {d.cli_version} 存在，但守护进程无响应")

    out.append(
        "  GPU         : " + ("；".join(report.gpu.devices) if report.gpu.available else "未检测到 NVIDIA GPU")
    )
    if report.disk.total_bytes:
        out.append(
            f"  磁盘        : {report.disk.path}  剩余 {report.disk.free_bytes / GB:.1f} GB "
            f"/ 共 {report.disk.total_bytes / GB:.1f} GB"
        )
    if report.ram.total_bytes:
        out.append(
            f"  内存        : {report.ram.total_bytes / GB:.1f} GB"
            f"（可用 {report.ram.available_bytes / GB:.1f} GB）"
            f"，建议 --shm-size={report.recommended_shm_bytes / GB:.1f}g"
        )
    if report.wsl_installed is not None:
        if report.wsl_installed:
            out.append("  WSL         : " + ("、".join(report.wsl_distros) or "已安装"))
        else:
            out.append("  WSL         : 未安装")

    out.append("")
    verdict = "可以执行量化" if report.can_quantize else "当前无法执行量化"
    out.append(f"  结论：{verdict}")
    out.append("")
    for f in report.findings:
        out.append(f"  {_ICON.get(f.level, f.level)} {f.message}")
        if f.hint:
            out.append(f"          → {f.hint}")
    out.append("")
    return "\n".join(out)


def _cmd_doctor(args: argparse.Namespace) -> int:
    required = None
    required_name = ""
    if args.image_id:
        spec = img.get_spec(args.image_id)
        info = img.probe_remote(spec.url)
        required = info.content_length or spec.size_hint_bytes
        required_name = spec.display_name

    report = probe_host(args.workspace, required_image_bytes=required, required_image_name=required_name)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_report(report))
        if not args.json:
            print(_render_images_table(remote=False))
    return 0 if report.can_quantize else 1


def _render_images_table(*, remote: bool, only_id: str | None = None) -> str:
    specs = img.load_manifest()
    local = set(img.local_images())
    rows: list[str] = []
    rows.append("可用工具链镜像")
    rows.append("-" * 86)
    rows.append(f"  {'ID':<16}{'板型':<10}{'架构':<12}{'变体':<6}{'体积':<12}{'本机'}")
    for spec in specs:
        if only_id and spec.id != only_id:
            continue
        size = spec.size_hint_bytes
        if remote:
            info = img.probe_remote(spec.url)
            size = info.content_length or size
        size_txt = img.human_bytes(size) if size else "未知"
        present = "已导入" if img.find_loaded(spec.image_ref_hint, local) else "—"
        rows.append(
            f"  {spec.id:<16}{spec.board:<10}{spec.march:<12}{spec.variant:<6}{size_txt:<12}{present}"
        )
    rows.append("-" * 86)
    rows.append("  提示：这些镜像**不是** docker pull 来的，而是官方离线 tar.gz；")
    rows.append("        `docker load` 之后以输出里的镜像名为准。")
    rows.append("")
    return "\n".join(rows)


def _cmd_images_list(args: argparse.Namespace) -> int:
    if args.json:
        data = []
        for spec in img.load_manifest():
            item = spec.__dict__.copy()
            if args.remote:
                info = img.probe_remote(spec.url)
                item["remote_size_bytes"] = info.content_length
                item["accept_ranges"] = info.accept_ranges
                item["reachable"] = info.ok
                if not info.ok:
                    item["remote_error"] = info.error
            data.append(item)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_render_images_table(remote=args.remote))
    return 0


def _cmd_images_fetch(args: argparse.Namespace) -> int:
    spec = img.get_spec(args.image_id)

    def on_line(line: str) -> None:
        print(line, flush=True)

    last = [0.0]

    def on_progress(done: int, total: int) -> None:
        if total <= 0:
            return
        pct = done * 100 // total
        if pct >= last[0] + 5 or done == total:
            last[0] = pct
            bar = "#" * (pct // 3)
            print(
                f"\r  [{bar:<33}] {pct:3d}%  {img.human_bytes(done)}/{img.human_bytes(total)}",
                end="",
                flush=True,
            )

    try:
        result = img.ensure_image(
            spec,
            cache_dir=args.cache_dir,
            on_line=on_line,
            on_progress=on_progress,
            prefer_pull=args.pull,
            keep_tar=not args.discard_tar,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n[失败] {exc}", file=sys.stderr)
        return 1
    print(f"\n完成：{result.action} → {result.image_ref}")
    return 0


def _cmd_mock(args: argparse.Namespace) -> int:
    """不依赖 Docker 地把假流程跑一遍，用来验证 core 的接线。"""
    ws = Workspace(Path(args.workspace)).ensure()
    backend = MockBackend(workspace=ws, march=args.march, delay=0.0)
    rc = backend.compile(on_line=lambda line: print(line))
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdkquant", description="RDK X3/X5 模型量化工具")
    parser.add_argument("--version", action="version", version="rdkquant 0.1.0")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_doctor = sub.add_parser("doctor", help="宿主机环境体检")
    p_doctor.add_argument("--workspace", default=".")
    p_doctor.add_argument("--image-id", default=None, help="按指定镜像评估磁盘/内存需求")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_images = sub.add_parser("images", help="工具链镜像管理")
    sub_images = p_images.add_subparsers(dest="image_cmd", required=True)

    p_list = sub_images.add_parser("list", help="列出可用镜像")
    p_list.add_argument("--remote", action="store_true", help="发 HEAD 请求取精确体积")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_images_list)

    p_fetch = sub_images.add_parser("fetch", help="下载并导入镜像")
    p_fetch.add_argument("image_id")
    p_fetch.add_argument(
        "--cache-dir",
        default=str(img.default_cache_dir()),
        help="离线包缓存目录（同一个包可复用于多台机器）",
    )
    p_fetch.add_argument("--pull", action="store_true", help="优先尝试 docker pull（不推荐）")
    p_fetch.add_argument("--discard-tar", action="store_true", help="导入后删除离线包")
    p_fetch.set_defaults(func=_cmd_images_fetch)

    p_mock = sub.add_parser("mock", help="用假后端跑通流程（无需 Docker）")
    p_mock.add_argument("--workspace", default="./demo_run")
    p_mock.add_argument("--march", default="bayes-e")
    p_mock.set_defaults(func=_cmd_mock)

    p_ui = sub.add_parser("ui", help="启动图形界面")
    p_ui.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    p_ui.set_defaults(func=_cmd_ui)

    return parser


def _cmd_ui(args: argparse.Namespace) -> int:
    try:
        from .ui.app import run
    except ImportError as exc:
        print("图形界面需要 PySide6：pip install 'rdkquant[ui]'", file=sys.stderr)
        print(f"（{exc}）", file=sys.stderr)
        return 1
    return run(workspace=Path(args.workspace))

def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args_list = list(sys.argv[1:] if argv is None else argv)

    # 不带参数 = 开图形界面。
    # 必须和打包产物（packaging/entry.py）行为一致：使用者的心智模型是
    # "双击/直接运行就是开界面"，不该被要求记住一个 ui 子命令。
    # 这里刻意用模块级名字调用 _cmd_ui，而不是走 args.func，
    # 这样它在单测里是可替换的。
    if not args_list:
        return _cmd_ui(argparse.Namespace(workspace=DEFAULT_WORKSPACE))

    parser = build_parser()
    args = parser.parse_args(args_list)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
