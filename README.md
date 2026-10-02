# RDK 模型量化工具

面向地平线 RDK X3 / X5 开发板的模型量化桌面工具。**图形界面跑在宿主机，量化在
Docker 容器里跑**——因为官方编译器只支持 x86 Linux，而且官方明确不建议把它装在
板子上。

流程严格对齐 D-Robotics 官方文档的章节顺序，界面上的每一步都能追溯到具体章节。

## 环境要求

| 项 | 要求 | 原因 |
|---|---|---|
| 系统 | **Ubuntu 22.04+ / Debian 12+**（glibc ≥ 2.34） | PySide6 6.11 的 Linux 轮子是 `manylinux_2_34` |
| 架构 | x86_64 | 官方工具链只有 x86_64 版本 |
| Python | 3.10 ~ 3.14 | `pyproject.toml` 里声明的范围 |
| Docker | 运行量化时需要 | 工具链以官方离线镜像分发 |
| 中文字体 | 建议装 `fonts-noto-cjk` | 缺了中文会显示成方块 |

> ⚠️ **不要把图形界面装进 OpenExplorer 容器。** 那个容器是 Ubuntu 20.04（glibc 2.31），
> 装不上 PySide6。正确形态就是「UI 在宿主机、`hb_mapper` 在容器里」，这也是本工具的
> 架构前提。

## 快速开始（Linux 桌面）

```bash
git clone <repo> rdk-quant && cd rdk-quant
./scripts/linux-setup.sh --run
```

脚本会依次：检查 glibc / Python → 用 apt 补齐 Qt 所需的系统库 → 建虚拟环境并安装
→ **做一次离屏渲染自检** → 检查图形环境 → 启动界面。

离屏自检这一步是刻意的：它把「代码/依赖有问题」和「图形环境没配好」分开。否则
xcb 一报错，你根本不知道是哪一头坏了。

装好之后启动：

```bash
.venv/bin/rdkquant ui
```

或者不看界面，先用命令行确认后端：

```bash
.venv/bin/rdkquant doctor            # 环境体检
.venv/bin/rdkquant images list --remote
```

## 快速开始（Windows 上用 WSL2）

图形界面会直接显示在 Windows 桌面上，不需要 VcXsrv 之类的 X 服务器——WSLg 自带。

**1. 装 WSL2**（管理员 PowerShell，之后重启一次）

```powershell
wsl --install -d Ubuntu-22.04
```

**2. 把项目放进 Linux 文件系统**（重要）

```bash
# 在 Ubuntu 里执行。不要放在 /mnt/c/... ——
#   1) 跨文件系统的 IO 很慢，pip 安装会慢到难以忍受
#   2) git 会因为属主不同报 "dubious ownership"
mkdir -p ~/src && cd ~/src
git clone <repo> rdk-quant && cd rdk-quant
```

如果你确实想继续用 Windows 上那份副本，至少先：

```bash
git config --global --add safe.directory /mnt/c/Users/<你>/Desktop/RDK-Quantization-UI
```

**3. 装好并启动**

```bash
./scripts/linux-setup.sh --run
```

**4. 量化环境（可选，等要用时再装）**

```bash
sudo apt-get install -y docker.io
sudo usermod -aG docker "$USER"     # 之后需要重新登录
.venv/bin/rdkquant images list --remote
.venv/bin/rdkquant images fetch x5-1.2.8-cpu
```

## 界面结构

```
┌───────────────────────────────────────────────────────────────┐
│ ①  ②  ③  ④  ⑤  ⑥  ⑦      ← 常驻流程步骤条（与官方章节一一对应）│
├──────────┬────────────────────────────────────────────────────┤
│ 导航栏    │  当前步骤（卡片流）                                  │
│ 环境准备  │  ┌──────────────────────────────────────────────┐  │
│ 模型准备  │  │ 该步的官方依据 / 要做什么 / 产出什么          │  │
│ 模型检查  │  └──────────────────────────────────────────────┘  │
│ 校准数据  │                                                    │
│ 量化编译  │                                                    │
│ 性能评估  │                                                    │
│ 精度评估  │                                                    │
│ ────────  │                                                    │
│ 工具箱    │                                                    │
├──────────┴────────────────────────────────────────────────────┤
│ 执行日志（可停靠，Ctrl+L 开关）· 进度                          │
└───────────────────────────────────────────────────────────────┘
```

步骤条与官方章节的对应关系是硬约束，写在 `core/workflow.py` 里：

| 步 | 官方依据 |
|---|---|
| 环境准备 | 环境部署（工具链首次使用做一遍） |
| 浮点模型准备 | 4.1.1.3（约束见 4.1.1.2 算子约束） |
| 模型检查 | 4.1.1.4 验证模型 |
| 校准数据准备 | 4.1.1.5 校准数据准备 |
| 模型量化与编译 | 4.1.1.6 模型量化与编译 |
| 性能评估 | 4.1.1.7 模型性能分析与调优 |
| 精度评估 | 4.1.1.8 模型精度分析与调优 |
| 工具箱（非步骤） | 4.1.1.9 其他模型工具（可选） |

> 概要说的是「五阶段」，详细章节把 `校准数据准备` 单列成 4.1.1.5，且要求它在
> makertbin **之前**完成。本工具采用详细章节序，把校准数据做成独立一步，
> 免得用户把校准集准备塞进「转换」按钮里。

## 排错

| 现象 | 原因与处理 |
|---|---|
| `Could not load the Qt platform plugin "xcb"` | 缺系统库。跑 `./scripts/linux-setup.sh --check`，它会把 `ldd` 里 `not found` 的库名列出来。最常见的是缺 `libxcb-cursor0` |
| 想看 Qt 到底在找什么 | `QT_DEBUG_PLUGINS=1 .venv/bin/rdkquant ui` |
| 界面里中文是方块 | `sudo apt-get install -y fonts-noto-cjk`；本工具也会在状态栏提示 |
| `no Qt platform plugin could be initialized` 且没有 DISPLAY | 当前是纯命令行会话。用 WSL2（WSLg 自带图形支持），或 `ssh -X`，或先起 Xvfb 再 VNC |
| 界面完全正常但量化跑不了 | Docker 没装/没起，或工具链镜像没导入。看「环境准备」页的体检结论 |
| Wayland 下行为怪异 | 试 `QT_QPA_PLATFORM=xcb .venv/bin/rdkquant ui` |
| pip 安装 PySide6 报 `not a supported wheel` | glibc < 2.34（Ubuntu 20.04）。升级系统，或改用分离部署 |

## 开发

```bash
.venv/bin/python -m pytest -q                    # 全部测试（含离屏 UI 冒烟测试）

# 截图矩阵：每个页面 × 主题 × 缩放各一张，用来评审界面
.venv/bin/python tools/shots.py --matrix --out shots
```

`tools/shots.py --matrix` 会含 **Fusion 兜底档**——那是 Linux 上最差的配置
（裸环境、没有桌面主题），在这里过关，在 KDE/Breeze 上只会更好。

### 目录约定

```
src/rdkquant/
  core/         业务层，**零 Qt 依赖**（有回归测试守着）
                —— 要能在 OpenExplorer 容器里独立跑
  ui/           界面层
    palette.py  颜色一律取自平台 QPalette，不硬编码
    qss.py      运行时由活的 palette 生成样式表
    metrics.py  尺寸令牌；与文字相关的尺寸按字体重算
    widgets/    自建组件（步骤条、卡片、指标块、日志…）
    pages/      每个流程步骤一页
  data/         镜像清单等数据（加板型只改数据，不改代码）
scripts/        Linux 环境准备
tools/          截图矩阵
```

界面层的两条硬规则：

1. **不引入第三方 UI 库**（`qfluentwidgets` / `superqt` / `BreezeStyleSheets` 都没用）。
   现代感来自排版与信息设计，颜色来自系统调色板——这样自动跟随明暗、强调色与
   高对比度设置，也不需要维护皮肤。
2. **不调 `app.setStyle("Fusion")`**。强制 Fusion 会顶掉 KDE 的原生 Breeze 绘制。

## 许可证

GPL-3.0-or-later，见 [LICENSE](LICENSE)。依赖许可证兼容性已核对：
PySide6 (LGPL-3)、onnx (Apache-2.0)、PyYAML (MIT) 均与 GPLv3 兼容。
