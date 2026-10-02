# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 RDK Quantization UI contributors

"""体检结论的最小载体。

原本这个类住在 `core/probe.py` 里，但"宿主机环境体检"和"模型/校准数据校验"
是两件事——让后者去 import 前者是分层错误。所以提到这里，
`core/probe.py` 原样 re-export 以保持既有 import 不变。
"""

from __future__ import annotations

from dataclasses import dataclass

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

#: 由重到轻，供 UI 排序用
LEVEL_ORDER = {LEVEL_ERROR: 0, LEVEL_WARN: 1, LEVEL_OK: 2}


@dataclass(frozen=True)
class Finding:
    """一条结论。

    `code` 是给程序看的稳定标识（测试与去重靠它），`message` 是给人看的一句话，
    `hint` 是"那该怎么办"。三者分开，UI 才能既有稳定的行为又有好的文案。
    """

    level: str
    code: str
    message: str
    hint: str = ""

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }

    @property
    def is_error(self) -> bool:
        return self.level == LEVEL_ERROR

    @property
    def is_warn(self) -> bool:
        return self.level == LEVEL_WARN
