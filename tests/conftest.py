# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""pytest 公共夹具。

`QT_QPA_PLATFORM=offscreen` 必须在 **QApplication 构造之前** 生效，所以放在
模块导入期设置，而不是夹具里——夹具跑起来时可能已经有别的测试建过 app 了。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("RDKQuant")
    app.setApplicationName("rdkquant-test")
    yield app
