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
| `core/` 不得导入 Qt | 它要能在 OE 容器（Ubuntu 20.04 / Python 3.8）里装而不拉进 Qt；有测试守着 |
| 宿主机**不碰像素**，宿主机没有图像库 | 缩略图、尺寸、曝光统计一律由容器侧的探针产出 |
| 探针必须 **Python 3.8 兼容** | X3 容器是 3.8（X5 是 3.10，已实测）。有 `ast.parse(feature_version=(3,8))` 测试守着 |
| 探针的 JSON 契约：**ASCII、最后一行 stdout** | 宿主靠扫最后若干行取回包。日志里的大块数据走**文件**，不走 stdout |
| `cal_data_dir` 是**无文件头的裸字节**，长度恰好 `W×H×C×itemsize` | `.npy` 会失败（多 128 字节文件头）。**不要"优化"成 .npy** |
| `hb_mapper checker` 必须显式传 `--march bayes-e` | 它的默认值是 `bayes`，不传就量化错芯片 |
| 容器以 **root** 运行，**不传 `--user`** | torch 在 `/root/.local/`，而 `/root` 是 700。传 `--user 1000:1000` 会让 `import torch` 失败 |
| 形状字符串用 **ASCII `x`** | 全角 `×` 会让探针的形状解析静默返回 `None`，于是字节数校验被悄悄跳过 |
| 容器内路径只能来自工作区 | 容器只挂载工作区。工作区**外**的目录必须先复制进来（模型、校准图片都是这么做的） |

### 已知的 Qt 陷阱（都真实踩过，别再踩）

- **`processEvents()` 不处理 `DeferredDelete`**。`deleteLater()` 只是排队，
  不投递就永远不销毁——测试里表现为控件累积、`apply_theme` 越来越慢（实测慢 14 倍）。
  需要 `QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)`。
- **`QListView` 在 IconMode 下只读模型的第 0 列**。用 `setItem(row, col)` 铺多列，
  只有每行第一个会显示出来。
- **`make_table(headers)` 收的是表头列表，不是 model**。传错不会抛异常，
  只会得到一张**全 0 宽的空表**。
- 信号连接要小心**立即求值**：`self.grid.invert_checked` 放进 tuple 会在构造期就取值。
- 构造期控件就会发 `valueChanged`，回调里碰还没创建的控件会 `AttributeError`。

### 文件操作

- **绝不用 PowerShell 的 `Get-Content` / `Set-Content` 改这些源码**：会写出乱码和 BOM
  （BOM 会让 Python 报 `invalid non-printable character U+FEFF`）。用编辑工具读写。
- 容器侧所有 `open()` 都要**显式 `encoding="utf-8"`**。不写就用平台编码，
  Windows 上是 GBK，遇到中文注释直接 `UnicodeDecodeError`。
- Windows 路径传给 Docker 要用 `C:/...` 形式（`normalize_host_path()` 负责）。

---

## 约束一：自动提交 Git

**完成一项改动就提交，不要攒着，也不要问。** 一个提交只做一件事。

提交信息用**中文**，格式：

```
<一句话说清修了什么>

<现象／用户报告的原话>

## 根因
<为什么坏。要具体到机制，不要写"逻辑错误">

## 取舍
问题：
选择：
替代方案与代价：

## 验证
<实测数字。命令 + 结果>
```

**必须用 `-F` 传文件，不能用 `-m`**：

```powershell
git add -A
git -c core.autocrlf=false commit -q -F "$env:TEMP\msg.txt"
```

多行中文信息用 `-m` 会被 PowerShell 拆散（`——` 之类的字符会触发参数分割，
实测报 `pathspec ... did not match any file(s) known to git`）。先把信息写进
临时文件（编辑工具写，保证 UTF-8），提交完删掉。

`core.autocrlf=false` 必须带：仓库里有 `.cmd` 和 shell 脚本，换行符被改写会坏。

提交完确认工作区干净：

```powershell
git status --short    # 应当无输出
```

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

代码改了，文档没改 = 文档在骗人。提交前检查：

| 文档 | 什么时候要改 |
|---|---|
| `AGENTS.md`（本文件） | 新增了架构级约束，或发现了一条新的"别再踩" |
| `README.md` | 安装／启动／目录约定／排错 变了 |
| `tests/fixtures/CONTAINER_FINDINGS.md` | 任何**实测的容器行为**被修正或补充 |

**特别检查"文档写的和代码相反"的情况。** 本项目已经出现过：文档第 7 节写着
「容器必须以 `--user` 运行」，而代码**故意不传 `--user`**（torch 在 700 权限的
`/root/.local` 下）。这种漂移比缺文档更危险，因为它会把人引向错误的修改。

发现漂移就**当场改掉**，并在提交信息里说明。

---

## 排错约定

- **先量，再改。** 本项目的多次绕远路都源于"看到一个可疑数字就以为找到了原因"。
  量的时候要量**假设的反面**（怀疑缓存慢，也要量"对象到底有没有被销毁"）。
- **临时诊断脚本放 `%TEMP%`**，不要留在仓库里。`tools/` 只放长期有用的工具。
- 实测结论要带数字。**"应该更快"不是结论，"169 秒 → 9.26 秒"才是。**
- 没用真机验证过的结论要**明说没验证**。宁可说"未验证"，不要说得像验证过。
