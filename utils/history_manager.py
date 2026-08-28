# -*- coding: utf-8 -*-
"""
通用历史记录管理器基类
负责处理各模块的历史记录存储和管理
"""

import os
import json
from pathlib import Path
from datetime import datetime
from abc import ABC, abstractmethod
from utils.logger import get_logger
from utils.error_handling import handle_errors

logger = get_logger('HistoryManager', 'WARNING')

# 测试存储重定向（tests/agent_contents 用）：设置后所有 HistoryManager 子类
# 写入 <override>/<prefix>/ 目录，替代各子类 get_memory_dir() 的真实目录。
# 默认 None → 真实系统零影响。
_test_storage_override: Path | None = None


def set_test_storage_override(root: Path | None) -> None:
    """设置/清除测试存储重定向根目录（None 恢复真实目录）。"""
    global _test_storage_override
    _test_storage_override = Path(root) if root is not None else None


def get_test_storage_override() -> Path | None:
    """获取当前测试存储重定向根目录（无则 None）。"""
    return _test_storage_override


class HistoryManager(ABC):
    """历史记录管理器基类
    
    子类需要实现以下抽象方法：
    - get_memory_dir(): 返回存储目录路径
    - get_file_prefix(): 返回文件名前缀
    """
    
    @handle_errors()
    def __init__(self, history_file=None):
        """初始化历史记录管理器
        
        Args:
            history_file: 历史记录文件名，None则使用'active.json'
        """
        self.memory_dir = self.get_memory_dir()
        # 测试存储重定向：设置后写入 <override>/<prefix>/，替代真实目录
        if _test_storage_override is not None:
            self.memory_dir = _test_storage_override / self.get_file_prefix()
        os.makedirs(self.memory_dir, exist_ok=True)
        self.history_file = self.memory_dir / (history_file or 'active.json')
        self._ensure_file_exists()
        self.archive_file_prefix = f'{self.get_file_prefix()}_archived_'
        self.max_archive_files = 10
    
    @abstractmethod
    def get_memory_dir(self) -> Path:
        """获取存储目录路径
        
        Returns:
            Path: 存储目录路径
        """
        pass
    
    @abstractmethod
    def get_file_prefix(self) -> str:
        """获取文件名前缀
        
        Returns:
            str: 文件名前缀
        """
        pass
    
    @handle_errors()
    def archive_context(self):
        """归档当前所有活跃历史记录并清空上下文"""
        history = self.get_history()
        if history:
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            archive_filename = f'{self.archive_file_prefix}{timestamp}.json'
            archive_file = self.memory_dir / archive_filename
            with open(archive_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            logger.info(f'已归档上下文到文件: {archive_file.name}')
            self._cleanup_old_archives()
            self.clear()
            logger.info(f'已将所有活跃历史记录归档，共 {len(history)} 条消息')
    
    @handle_errors()
    def _ensure_file_exists(self):
        """确保存储历史记录的文件存在"""
        if not self.history_file.exists():
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump([], f)
    
    @handle_errors()
    def _cleanup_old_archives(self):
        """清理旧的归档文件，确保不超过数量上限"""
        archive_files = list(self.memory_dir.glob(f'{self.archive_file_prefix}*.json'))
        archive_files.sort(key=os.path.getmtime)
        if len(archive_files) > self.max_archive_files:
            files_to_delete = len(archive_files) - self.max_archive_files
            for file in archive_files[:files_to_delete]:
                os.remove(file)
                logger.info(f'已删除旧归档文件: {file.name}')
    
    @handle_errors()
    def append(self, message: dict):
        """保存历史记录到文件：追加新消息并原子性替换文件"""
        with open(self.history_file, 'r', encoding='utf-8') as f:
            history = json.load(f)
        history.append(message)
        temp_file = self.history_file.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        self._atomic_replace(temp_file, self.history_file)
        logger.debug('历史记录保存成功')

    def _atomic_replace(self, src: Path, dst: Path):
        """原子性替换文件，处理 Windows 文件锁定问题"""
        import time
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt < max_attempts - 1:
                    time.sleep(0.1)
                else:
                    try:
                        if dst.exists():
                            os.remove(dst)
                        os.replace(src, dst)
                    except Exception:
                        raise

    @handle_errors()
    def get_history(self):
        """从文件中获取历史记录：返回JSON格式的历史记录列表"""
        with open(self.history_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    @handle_errors()
    def clear(self):
        """清空历史记录：将文件内容重置为空列表"""
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump([], f)
        logger.warning('历史记录已清空')
