#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据库公共模块

提供节点 ID 生成等公共工具函数
"""

import time

def generate_timestamp_id(prefix: str) -> str:
    """生成时间戳ID，格式：前缀_时间戳_微秒
    
    Args:
        prefix: 节点类型前缀，如 Character, Group, Event 等
        
    Returns:
        唯一ID字符串，格式：前缀_YYYYmmddHHMMSSmmm
    """
    t = time.time()
    ms = int((t - int(t)) * 1000)
    return f"{prefix}_{time.strftime('%Y%m%d%H%M%S')}{ms:03d}"
