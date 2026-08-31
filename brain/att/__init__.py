# -*- coding: utf-8 -*-
"""
注意力模块入口

API 文档：docs/api/attention.md —— 修改本包对外接口后必须同步更新该文档
（校验：python scripts/check_api_docs.py）。
"""

# 导出注意力核心工厂函数
from .att_core import get_attention_core
