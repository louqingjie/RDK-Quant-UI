# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""RDK 模型量化工具。

设计要点：
  * core 层零 Qt 依赖，可以在容器/无头环境里独立运行与测试。
  * 量化在 Docker 容器内执行，UI 在宿主机运行，两者通过挂载目录交换文件。
"""

__version__ = "0.1.0"
