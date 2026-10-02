# RDK 模型量化工具

面向地平线 RDK X3 / X5 开发板的模型量化桌面工具。**图形界面跑在宿主机，量化在
Docker 容器里跑**——因为官方编译器只支持 x86 Linux，而且官方明确不建议把它装在
板子上。

流程严格对齐 D-Robotics 官方文档的章节顺序，界面上的每一步都能追溯到具体章节。

## 使用

**本工具以自包含程序分发：使用者不需要 Python、不需要 pip、不需要虚拟环境。**

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | **Ubuntu 22.04+ / Debian 12+**（glibc ≥ 2.34） | 低于此版本跑不了，见下方「为什么」 |
| 架构 | x86_64 | 官方工具链只有 x86_64 版本 |
| Docker | **只有做量化时才需要** | 纯看界面、准备模型、检查配置都不需要它 |
| 中文字体 | 建议装 `fonts-noto-cjk` | 缺了中文会显示成方块 |

```bash
tar xf rdkquant-<版本>-linux-x86_64.tar.gz
cd rdkquant
./rdkquant            # 图形界面
./rdkquant doctor     # 环境体检（命令行，不需要显示器）
./rdkquant images …   # 镜像管理
```

同一个可执行文件既能开界面也能当命令行工具，不用记两个名字。

> **为什么不能「让用户自己装」？** 两条硬约束：
> 1. Ubuntu 22.04 的仓库里**根本没有 PySide6**（只有 PySide2/Qt5），
>    `apt install python3-pyside6` 无解；
> 2. Ubuntu 23.04+ / Debian 12+ 用 PEP 668 锁掉了系统 pip，`pip install` 会被直接拒绝。
>
> 所以环境只能由我们带进去。用户机器上装了什么 Python、有没有 venv，与本工具无关。

> ⚠️ **不要把图形界面装进 OpenExplorer 容器。** 那个容器是 Ubuntu 20.04（glibc 2.31），
> 装不上 PySide6。正确形态就是「UI 在宿主机、`hb_mapper` 在容器里」，这也是本工具的
> 架构前提。

## 从源码构建（开发者）

如果你要改代码，或者想自己出这个自包含程序：

```bash
git clone <repo> rdk-quant && cd rdk-quant
./scripts/build-app.sh          # 产出 dist/rdkquant/，167 MB 左右
./dist/rdkquant/rdkquant        # 直接跑，不需要激活任何环境
```

`build-app.sh` 内部**确实**会建一个虚拟环境，但它是构建期的实现细节，放在
`.build/` 下，对使用者完全不可见——这就是「把 venv 从用户路径里拿掉」的具体做法：
不是删掉 venv，而是不让用户碰它。

### 日常开发

开发时才需要环境管理。`dev-setup.sh` 会建 `.venv`、装 dev 依赖、跑离屏自检：

```bash
./scripts/dev-setup.sh --run     # 装好并启动界面
./scripts/dev-setup.sh --check   # 只诊断，不改动系统
```

启动后：`.venv/bin/rdkquant ui`，命令行用法同上。

## 在 Windows 上用 WSL2

图形界面会直接显示在 Windows 桌面上，不需要 VcXsrv 之类的 X 服务器——WSLg 自带。

**1. 装 WSL2**（管理员 PowerShell，之后重启一次）

```powershell
wsl --install -d Ubuntu-22.04
```

**2. 把项目放进 Linux 文件系统**（重要）

```bash
# 在 Ubuntu 里执行。开发用的仓库不要放在 /mnt/c/... ——
#   1) 跨文件系统 IO 很慢，pip 安装会慢到难以忍受
#   2) 该挂载不支持 chmod，python3 -m venv 会直接失败
#   3) git 会因为属主不同报 "dubious ownership"
mkdir -p ~/src && cd ~/src
git clone <repo> rdk-quant && cd rdk-quant
```

（只是**使用**的话不受这条限制：自包含程序放在哪儿都能跑，它不需要 chmod。）

**3. 装好并启动**

```bash
./scripts/dev-setup.sh --run
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
| **`ensurepip returned non-zero exit status 1`**，或 `[Errno 1] Operation not permitted: .../bin/activate.fish` | **在 WSL 的 `/mnt/c` 上跑。** 该挂载是 `uid=0;gid=0`，所有文件都算 root 所有，非 root 用户 `chmod` 会 EPERM，而 `python3 -m venv` 必须给 activate 脚本 chmod。**把仓库放到 Linux 文件系统里**（`~/src/...`）。脚本现在会提前拦下并给出确切命令 |
| `Could not load the Qt platform plugin "xcb"` | 缺系统库。跑 `./scripts/linux-setup.sh --check`，它会把 `ldd` 里 `not found` 的库名列出来。最常见的是缺 `libxcb-cursor0` |
| `python3 -m venv` 提示 `ensurepip is not available` | 缺 `python3-venv`——**最小化的 Ubuntu/WSL 默认没装**。`sudo apt-get install -y python3-venv`（脚本现在会自动装） |
| `apt-get update` 长时间没反应 | 默认的 `archive.ubuntu.com` 在国内常常不可用。换成清华/阿里镜像源再试 |
| 要看 Qt 到底在找什么 | `QT_DEBUG_PLUGINS=1 .venv/bin/rdkquant ui` |
| 界面里中文是方块 | `sudo apt-get install -y fonts-noto-cjk`；本工具也会在状态栏提示 |
| `no Qt platform plugin could be initialized` 且没有 DISPLAY | 当前是纯命令行会话。用 WSL2（WSLg 自带图形支持），或 `ssh -X`，或先起 Xvfb 再 VNC |
| 界面完全正常但量化跑不了 | Docker 没装/没起，或工具链镜像没导入。看「环境准备」页的体检结论 |
| Wayland 下行为怪异 | 试 `QT_QPA_PLATFORM=xcb .venv/bin/rdkquant ui` |
| pip 安装 PySide6 报 `not a supported wheel` | glibc < 2.34（Ubuntu 20.04）。升级系统，或改用分离部署 |
| Docker 版本被体检判为太旧 | `--workdir` 需要 17.09+，`--gpus` 需要 19.03+。装 `docker.io` 或 `docker-ce` 均可，见上文选型说明 |

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
