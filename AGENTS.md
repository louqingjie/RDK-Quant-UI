# AGENTS.md

## 项目

RDK 模型量化工具：把地平线 RDK X5 / X3 的模型量化流程（官方文档 4.1.1.3–4.1.1.8）
做成桌面 GUI。**容器是唯一的执行环境**，宿主机只做编排。

- `src/rdkquant/core/` —— 纯标准库，零 Qt。负责调用容器、解析日志、判定策略。
- `src/rdkquant/ui/` —— PySide6。纯原生 Qt Widgets，调色板驱动主题。
- `src/rdkquant/data/rdkquant_probe.py` —— **容器侧探针**，写进工作区再执行，
  不是被导入的模块。它有自己的兼容性约束（见下）。

七个流程步骤：环境准备 → 浮点模型准备 → 模型检查 → 校准数据准备 →
模型量化与编译 → 性能评估 → 精度评估。

---

## 约束

1. `core/` 不得导入 Qt
2. **图像处理放宿主机**，用 Qt（`ui/calibimages.py`） 
3. 但**生成校准数据的二进制仍在容器里** 
4. 图像代码只在 `ui/` 里，`core/` 保持零 Qt
5. 探针必须 **Python 3.8 兼容**
6. 探针的 JSON 契约：**ASCII、最后一行 stdout**
7. `cal_data_dir` 是**无文件头的裸字节**，长度恰好 `W×H×C×itemsize`
8. `hb_mapper checker` 必须显式传 `--march bayes-e`
9. 容器以 **root** 运行，**不传 `--user`**
10. 形状字符串用 **ASCII `x`**
11. 容器内路径只能来自工作区
12. 改 `src/rdkquant/ui/` 前先看 [`docs/pitfalls/qt-traps.md`](docs/pitfalls/qt-traps.md)。
13. 绝不用 PowerShell 的 `Get-Content` / `Set-Content` 改这些源码
14. 容器侧所有 `open()` 都要**显式 `encoding="utf-8"`**。
15. Windows 路径传给 Docker 要用 `C:/...` 形式（`normalize_host_path()` 负责）。
16. 自动提交 Git
17. 提交前检查：**在 Windows 和 Linux 两边都跑全量测试**。两个平台的失败模式不同，只跑一边会漏。有失败**不允许提交**。

---

## 文档

`docs/` 按用途分目录，**写之前看 [`docs/README.md`](docs/README.md)** —— 分工表、命名、写法都在那儿。

取舍和决定进 `docs/adr/`；踩过的坑进 `docs/pitfalls/`；实测出来的环境事实进 `docs/design/`。

改代码让哪句话变错了，**就在同一个提交里改**；没有就在提交信息写一行「文档：无」。