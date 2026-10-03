# AGENTS.md

## 项目

RDK 模型量化工具：把地平线 RDK X5 / X3 的模型量化流程（官方文档 4.1.1.3–4.1.1.8）
做成桌面 GUI。**容器是唯一的执行环境**，宿主机只做编排。

- `src/rdkquant/core/` —— 纯标准库，零 Qt。负责调用容器、解析日志、判定策略。
- `src/rdkquant/ui/` —— PySide6。纯原生 Qt Widgets，调色板驱动主题。
- `src/rdkquant/data/rdkquant_probe.py` —— **容器侧探针**，写进工作区再执行，
  不是被导入的模块。它有自己的兼容性约束（见下）。
- 许可证 GPL-3.0-or-later。

七个流程步骤：环境准备 → 浮点模型准备 → 模型检查 → 校准数据准备 →
模型量化与编译 → 性能评估 → 精度评估。

---

## 架构铁律

这些不是风格偏好，是**踩过坑之后定下来的约束**。改动前先想清楚为什么有这条。

| 约束 | 理由 |
|---|---|
| `core/` 不得导入 Qt | 它要能在 OE 容器（Ubuntu 20.04 / Python 3.8）里装而不拉进 Qt；有测试守着。**图像能力因此只能放 `ui/`** |
| **图像处理放宿主机**，用 Qt（`ui/calibimages.py`） | 读文件头、缩略图是**界面要用的东西**，不是量化工作。实测 120 张缩略图：宿主机 0.71 秒 vs 容器 4.96 秒（7x）。**不需要 Pillow** —— PySide6 自带图像栈 |
| 但**生成校准数据的二进制仍在容器里** | `calib-build` 的字节契约（`W×H×C×itemsize`）应当在**消费它的那个环境**里校验；而且它是批处理，慢一点无所谓 |
| 图像代码只在 `ui/` 里，`core/` 保持零 Qt | 见下一条 |
| 探针必须 **Python 3.8 兼容** | X3 容器是 3.8（X5 是 3.10，已实测）。有 `ast.parse(feature_version=(3,8))` 测试守着 |
| 探针的 JSON 契约：**ASCII、最后一行 stdout** | 宿主靠扫最后若干行取回包。日志里的大块数据走**文件**，不走 stdout |
| `cal_data_dir` 是**无文件头的裸字节**，长度恰好 `W×H×C×itemsize` | `.npy` 会失败（多 128 字节文件头）。**不要"优化"成 .npy** |
| `hb_mapper checker` 必须显式传 `--march bayes-e` | 它的默认值是 `bayes`，不传就量化错芯片 |
| 容器以 **root** 运行，**不传 `--user`** | torch 在 `/root/.local/`，而 `/root` 是 700。传 `--user 1000:1000` 会让 `import torch` 失败 |
| 形状字符串用 **ASCII `x`** | 全角 `×` 会让探针的形状解析静默返回 `None`，于是字节数校验被悄悄跳过 |
| 容器内路径只能来自工作区 | 容器只挂载工作区。工作区**外**的目录必须先复制进来（模型、校准图片都是这么做的） |

### 已知的 Qt 陷阱

改 `src/rdkquant/ui/` 前先看 [`docs/qt-traps.md`](docs/qt-traps.md)。六条，都真实踩过：

1. `processEvents()` 不处理 `DeferredDelete` → 控件从不销毁（曾让整个套件慢 14 倍）
2. `QListView` 的 IconMode 只读模型的第 0 列 → 只显示每行第一个
3. `make_table(headers)` 收的是表头列表，不是 model → 传错得到全 0 宽空表
4. 信号连接会被**立即求值** → 构造期 `AttributeError`
5. 构造期控件就发 `valueChanged` → 回调里碰还没创建的控件
6. 跨线程：`Signal` 参数写 `dict` 发不出去（静默丢失）；`QPixmap`/`QIcon` 只能在 GUI 线程造

**第 3~6 条出错时没有异常**，只是"看起来不对"。所以改完 UI 要**看渲染结果**，不能只看"没报错"。

其中**可机械判定**的那部分由 [`tests/uigeom.py`](tests/uigeom.py) 守着：条目装不下图标+文字
（真发生过——只有 14px 高，缩略图压住文件名和勾选框）、可见控件 0 尺寸、文案漏出
`None`/`Traceback`。**它抓不到两类**：代码路径没被走到、文案和代码相反——这两类本会话
各发生过一次，都是看截图发现的。**别因为自检绿了就跳过看图。**

### 文件操作

- **绝不用 PowerShell 的 `Get-Content` / `Set-Content` 改这些源码**：会写出乱码和 BOM
  （BOM 会让 Python 报 `invalid non-printable character U+FEFF`）。用编辑工具读写。
- 容器侧所有 `open()` 都要**显式 `encoding="utf-8"`**。不写就用平台编码，
  Windows 上是 GBK，遇到中文注释直接 `UnicodeDecodeError`。
- Windows 路径传给 Docker 要用 `C:/...` 形式（`normalize_host_path()` 负责）。

---

## 约束一：自动提交 Git

**完成一项改动就提交，不要攒着，也不要问。** 一个提交只做一件事。

提交信息用**中文**，四段：现象 → 根因 → 取舍 → 验证。

```powershell
git add -A
git -c core.autocrlf=false commit -q -F "$env:TEMP\msg.txt"
```

**必须 `-F` 传文件，不能用 `-m`** —— 多行中文会被 PowerShell 拆散（`——` 之类的
字符触发参数分割，实测报 `pathspec ... did not match any file(s) known to git`）。
`core.autocrlf=false` 也必须带：仓库里有 `.cmd` 和 shell 脚本，换行被改写会坏。

完整模板、逐段说明和「为什么不用 `-m`」见 [`docs/commit-format.md`](docs/commit-format.md)。
提交完 `git status --short` 应当无输出。

---

## 约束二：提交前检查

**在 Windows 和 Linux 两边都跑全量测试**。两个平台的失败模式不同，只跑一边会漏。

```powershell
# Windows
.\.venv\Scripts\python.exe -m pytest -q

# Linux（先把 Windows 侧改动同步过去）
wsl -e bash -lc "cd /mnt/c/Users/ccron/Desktop/RDK-Quantization-UI && bash ./scripts/wsl-sync.sh && cd ~/src/rdk-quant && QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q"
```

- 有失败**不允许提交**。测试挂了说明它发现了真问题——**不要改测试让它变绿**，
  除非能说清是测试本身的断言错了（这种情况要在提交信息里写明）。
- 契约测试（`test_backends.py` 的 `MOCK_*` 与真实探针比对）失败时，
  要么改 Mock 跟上，要么**同时**改真实探针和契约集，不能只改一边。
- Windows 上 `test_interrupt.py` 会跳过（POSIX-only），这是预期的。

新增行为要有测试。**写断言时小心假通过**——比如验证路径拼接要用相对路径，
用绝对路径会绕过真正的失败模式。

---

## 约束三：重要取舍必须记录下来

遇到有多个合理做法的决策，**不要默默选一个**。记录四件事：

| 项 | 要求 |
|---|---|
| **问题** | 要解决什么。带上实测数据（"3504 张、1474 种分辨率"比"数据量大"有用） |
| **选择** | 选了什么，一句话 |
| **替代方案** | 还考虑过什么，为什么没选 |
| **代价** | 选择带来的**坏处**。没有代价的方案通常是没想清楚 |

写在哪里：

- **提交信息**里（主要位置）。用上面格式里的「取舍」段。
- **`tests/fixtures/CONTAINER_FINDINGS.md`** —— 凡是**实测出来的容器行为**
  （格式、默认值、路径、报错），必须落进这个文件。它是防止"凭想象写代码"的锚点。

代价要写成**后来者能据此重新决策**的样子。反例：「这样比较简洁」。
正例：「代价：每次都要重渲染 SVG，整个套件从 60 秒变成 170 秒——产品不受影响，
因为一个进程只建一个 AppShell」。**后来发现这个代价其实是 bug 造成的**，
所以代价描述得具体，才有机会被推翻。

---

## 约束四：提交前更新文档

代码改了，文档没改 = 文档在骗人。提交前过三个问题：

1. **这次改的东西，有哪份文档描述过？** 有就去核对。
2. **这次发现了什么"原来是这样"的事？** 实测的容器行为进
   `tests/fixtures/CONTAINER_FINDINGS.md`，踩坑经验进本文件。
3. **有没有哪句话和代码说的是反的？**

文档更新和代码改动**放在同一个提交里**。

**具体哪份文档该放什么、怎么写，看 [`docs/AGENTS.md`](docs/AGENTS.md)** —— 那里是
那份规格的家，这里不重复。

### 特别检查"文档写的和代码相反"

这比缺文档危险：缺文档只是没人看，**写反的文档会把人引向错误的修改**。
处置原则一句话：**先假设代码是对的**（它经过实测），去改文档。
本项目发生过一次，完整经过和理由见 [`docs/AGENTS.md`](docs/AGENTS.md#文档和代码相反)。

---

## 排错约定

- **先量，再改。** 本项目的多次绕远路都源于"看到一个可疑数字就以为找到了原因"。
  量的时候要量**假设的反面**（怀疑缓存慢，也要量"对象到底有没有被销毁"）。
- **临时诊断脚本放 `%TEMP%`**，不要留在仓库里。`tools/` 只放长期有用的工具。
- 实测结论要带数字。**"应该更快"不是结论，"169 秒 → 9.26 秒"才是。**
- 没用真机验证过的结论要**明说没验证**。宁可说"未验证"，不要说得像验证过。
