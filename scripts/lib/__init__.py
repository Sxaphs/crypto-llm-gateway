"""crypto-llm-mvp 运维脚本共享库。

只依赖 Python 标准库（HTTP 用 urllib），不 import adapter 内部代码，
保证每个脚本都能脱离应用代码单独运行。金额计算统一使用 decimal.Decimal。
"""
from __future__ import annotations

__version__ = "0.1.0"
