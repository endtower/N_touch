# -*- coding: utf-8 -*-
"""
通用历史记录管理器基类（事件链存储模型）

存储结构（所有上下文统一收敛到 memory/event_chains/）::

    memory/event_chains/
      event_chain_20260912_223001_ab12/     ← 每个任务链一个文件夹（链 ID 含时间戳，天然按时间排序）
        pfca_223045.json                    ← 链内一次唤醒 = 一个上下文文件（{prefix}_{HHMMSS}.json）
        conversation_223051.json               同一 agent 多次唤醒产生多个文件，按文件名
        conversation_224112.json               （即创建时间）排序，供前端"上下文N"编号展示
        social_chat_223058.json
      event_chain_20260911_.../
      unlinked/                             ← 未关联链（链外写入 + legacy 迁移）
        conversation.json                   ← 链外常驻写入（累积，由归档轮转清空）
        pfca_223001.json                    ← 链外归档（带时间戳）
        conversation_archived_*.json        ← 旧格式迁移保留原名

核心语义:
  - 写入即落位: append 时根据 contextvar 中的 chain_id 直接写入链文件夹内
    本次唤醒的独立文件（{prefix}_{HHMMSS}.json，唤醒期间缓存，永不重命名）。
  - 一次唤醒一个文件: 归档后下次唤醒生成新文件 -> 链内条目按创建时间排列，
    天然还原"唤醒 1 / 唤醒 2 / ..."的分段视图。
  - 内存/磁盘分离: get_history() 只返回本次唤醒的内存会话历史
    (_session_history)，归档后下次唤醒从空上下文开始；磁盘链文件是展示用的
    完整记录，仅供按链查看，不参与 LLM 请求组装。
  - 归档 = 终结: archive_context 只终结写入资格（注册表 + 广播）并失效唤醒
    文件缓存，链文件夹内文件原位保留，前端按链文件夹查看整个任务的全部上下文。
  - 链外写入: 写入 unlinked/{prefix}.json 常驻文件；归档时轮转为
    unlinked/{prefix}_{HHMMSS}.json（这是链外单文件共享场景的唯一例外）。
  - 旧存档迁移: 各 manager 首次实例化时一次性把旧格式（{prefix}_chain_* /
    {prefix}_archived_* / 旧 active 文件 / 链内残留固定名文件）迁入新结构，幂等。
"""

import asyncio
import os
import json
import re
import shutil
from pathlib import Path
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Optional, Set
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


# ============================================================
# 进行中链注册表
# 供 /context API 判定链文件夹是否处于"进行中"（仍在被写入）。
# ============================================================

_active_chains: Set[str] = set()

# 链文件夹保留上限（按文件夹 mtime 保留最近 N 个，防止无限膨胀）
MAX_CHAIN_DIRS = 20

# 旧链归档文件名: {prefix}_chain_{chain_id}_{HHMMSS}.json（迁移解析用）
# chain_id 本身含下划线, (.+)_ greedy 匹配到倒数第二段, 末段固定 6 位数字
_LEGACY_CHAIN_NAME_RE = re.compile(r'^[a-z]+_chain_(.+)_([0-9]{6})\.json$')

# 已完成迁移的 (memory_dir, prefix) 集合（防重复迁移）
_migrated_keys: set = set()


def is_chain_active(chain_id: str) -> bool:
    """查询某个链是否处于进行中（仍在被写入）。"""
    return chain_id in _active_chains


def get_active_chain_ids() -> Set[str]:
    """获取全部进行中的链 ID 集合（只读副本）。"""
    return set(_active_chains)


# HistoryManager 前缀 -> 前端 kind（/context API 的 kind 命名）
_PREFIX_TO_KIND = {
    'conversation': 'chat',
    'observe': 'chat',
    'temporary': 'chat',
    'pfca': 'pfca',
    'proactive': 'proactive',
    'social_chat': 'social_chat',
}


def _fire_context_updated_broadcast(prefix: str, event: str, chain_id: Optional[str]) -> None:
    """落盘后 fire-and-forget 广播 context_updated（测试重定向模式下跳过）。

    仿 frontend_broadcast._broadcast_activity 先例：create_task 调度、全静默，
    广播失败不影响主流程（前端另有 3s 轮询兜底）。
    """
    if _test_storage_override is not None:
        return
    kind = _PREFIX_TO_KIND.get(prefix, prefix)

    async def _do():
        try:
            from adapt.manager import get_channel_manager
            mgr = await get_channel_manager()
            web_ch = mgr.get('web')
            if web_ch is None:
                return
            await web_ch.broadcast_context_updated(kind, event, chain_id)
        except Exception as e:
            logger.debug(f'broadcast_context_updated 跳过: {e}')

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do())
    except RuntimeError:
        pass


class HistoryManager(ABC):
    """历史记录管理器基类

    子类需要实现以下抽象方法：
    - get_memory_dir(): 返回存储目录路径
    - get_file_prefix(): 返回文件名前缀
    """

    @handle_errors()
    def __init__(self):
        """初始化历史记录管理器。

        注意: 子类必须在自己 __init__ 中先设置好 get_file_prefix() 依赖的
        实例状态（如 Chat_HistoryManager 的 self.prefix），再调用 super().__init__()。
        """
        self.memory_dir = self.get_memory_dir()
        # 测试存储重定向：设置后写入 <override>/<prefix>/，替代真实目录
        if _test_storage_override is not None:
            self.memory_dir = _test_storage_override / self.get_file_prefix()
        # 事件链存储根: <memory>/event_chains/（跨 kind 汇聚，按链分文件夹）
        self.chains_root = self.memory_dir.parent / 'event_chains'
        # 链外常驻写入文件（unlinked），固定名 {prefix}.json，永不重命名
        self.history_file = self.chains_root / 'unlinked' / f'{self.get_file_prefix()}.json'
        # 链外常驻文件的归档轮转上限（对齐旧归档上限语义）
        self.max_archive_files = 10
        # 本 manager 最近一次参与链写入的 chain_id（归档终结时用）
        self.current_chain_id = None
        # 本次唤醒的内存会话历史（LLM 上下文）；磁盘文件是跨唤醒的展示累积
        self._session_history: list = []
        # 本次唤醒在链内的落盘文件（{prefix}_{HHMMSS}.json，唤醒期间缓存，
        # 归档后失效 -> 下次唤醒生成新文件，实现链内"一次唤醒一个上下文条目"）
        self._session_file: Optional[Path] = None
        self._session_chain_id: Optional[str] = None
        self._ensure_file_exists()
        self._migrate_legacy_files()

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

    # --------------------------------------------------------
    # 路径定位
    # --------------------------------------------------------

    def _current_target_file(self) -> Path:
        """定位当前写入/读取目标文件。

        - 链内（contextvar 有 chain_id）: 本次唤醒的独立落盘文件
          event_chains/{chain_id}/{prefix}_{HHMMSS}.json（唤醒期间缓存，
          归档后失效）——链内一次唤醒 = 一个上下文文件
        - 链外: event_chains/unlinked/{prefix}.json（常驻）
        """
        from utils.chain_context import get_current_chain_id
        chain_id = get_current_chain_id()
        if chain_id:
            if self._session_file is None or self._session_chain_id != chain_id:
                self._session_file = self._new_chain_session_file(chain_id)
                self._session_chain_id = chain_id
            return self._session_file
        return self.history_file

    def _new_chain_session_file(self, chain_id: str) -> Path:
        """为本次唤醒生成链内落盘文件路径（{prefix}_{HHMMSS}.json，冲突加序号）。"""
        chain_dir = self.chains_root / chain_id
        chain_dir.mkdir(parents=True, exist_ok=True)
        prefix = self.get_file_prefix()
        timestamp = datetime.now().strftime('%H%M%S')
        candidate = chain_dir / f'{prefix}_{timestamp}.json'
        seq = 1
        while candidate.exists():
            seq += 1
            candidate = chain_dir / f'{prefix}_{timestamp}_{seq}.json'
        return candidate

    # --------------------------------------------------------
    # 旧存档一次性迁移（幂等）
    # --------------------------------------------------------

    @handle_errors()
    def _migrate_legacy_files(self):
        """把旧格式存档迁入 event_chains/ 结构（每个 (dir, prefix) 只跑一次）。

        - {prefix}_chain_{chain}_{HHMMSS}.json → event_chains/{chain}/{prefix}.json
          （同链同前缀多份时, 最新一份占固定名, 其余保留 {prefix}_{HHMMSS}.json）
        - {prefix}_archived_*.json（legacy 无链归档）→ event_chains/unlinked/ 保留原名
        - 旧 active 文件非空 → 内容搬移为 unlinked/{prefix}_legacy_{ts}.json, 源文件删除
        """
        key = (str(self.memory_dir.resolve()), self.get_file_prefix())
        if key in _migrated_keys:
            return
        _migrated_keys.add(key)
        prefix = self.get_file_prefix()
        self.chains_root.mkdir(parents=True, exist_ok=True)
        unlinked_dir = self.chains_root / 'unlinked'
        unlinked_dir.mkdir(parents=True, exist_ok=True)

        # 1) 旧链归档 → 对应链文件夹（落时间戳名, 不占用固定名）
        for f in sorted(self.memory_dir.glob(f'{prefix}_chain_*.json'), key=os.path.getmtime):
            m = _LEGACY_CHAIN_NAME_RE.match(f.name)
            if not m:
                logger.warning(f'迁移跳过无法解析的链归档文件: {f.name}')
                continue
            chain_id, hhmmss = m.group(1), m.group(2)
            chain_dir = self.chains_root / chain_id
            chain_dir.mkdir(parents=True, exist_ok=True)
            target = chain_dir / f'{prefix}_{hhmmss}.json'
            seq = 1
            while target.exists():
                seq += 1
                target = chain_dir / f'{prefix}_{hhmmss}_{seq}.json'
            f.replace(target)
            logger.info(f'迁移链归档: {f.name} -> {target.relative_to(self.chains_root)}')

        # 2) legacy 无链归档 → unlinked/ 保留原名
        for f in sorted(self.memory_dir.glob(f'{prefix}_archived_*.json'), key=os.path.getmtime):
            f.replace(unlinked_dir / f.name)
            logger.info(f'迁移 legacy 归档: {f.name} -> unlinked/')

        # 3) 旧 active 文件（重启遗留的链外内容）→ unlinked/{prefix}_legacy_{ts}.json
        for old_name in (f'{prefix}.json', 'active.json'):
            old = self.memory_dir / old_name
            if not old.exists():
                continue
            try:
                with open(old, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = None
            if isinstance(data, list) and data:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                target = unlinked_dir / f'{prefix}_legacy_{ts}.json'
                old.replace(target)
                logger.info(f'迁移旧 active 内容: {old_name} -> {target.name}')
            else:
                old.unlink(missing_ok=True)

        # 4) 链内残留固定名 {prefix}.json → {prefix}_legacy_{ts}.json（或删除空文件）
        #    唤醒分段展示模型下链内固定名不再使用; 重启后跑一次, 幂等
        for chain_dir in self.chains_root.iterdir():
            if not chain_dir.is_dir() or chain_dir.name == 'unlinked':
                continue
            fixed = chain_dir / f'{prefix}.json'
            if not fixed.exists():
                continue
            try:
                with open(fixed, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = None
            if isinstance(data, list) and data:
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                target = chain_dir / f'{prefix}_legacy_{ts}.json'
                seq = 1
                while target.exists():
                    seq += 1
                    target = chain_dir / f'{prefix}_legacy_{ts}_{seq}.json'
                fixed.replace(target)
                logger.info(f'链内固定名迁移: {chain_dir.name}/{fixed.name} -> {target.name}')
            else:
                fixed.unlink(missing_ok=True)

    # --------------------------------------------------------
    # 归档
    # --------------------------------------------------------

    @handle_errors()
    def archive_context(self):
        """归档当前上下文。

        - 链内（本 manager 参与过链写入）: 链文件夹内文件原位保留（供按链查看），
          仅终结写入资格并广播，同时清空内存会话历史（下次唤醒从干净上下文
          开始）；随后触发链文件夹数量清理。
        - 链外: 轮转 unlinked/{prefix}.json 常驻文件（内容搬到
          unlinked/{prefix}_{HHMMSS}.json 并清空常驻），空文件直接跳过。
        """
        chain_id = self.current_chain_id
        if chain_id:
            self.current_chain_id = None
            self._session_history = []
            self._session_file = None
            self._session_chain_id = None
            _active_chains.discard(chain_id)
            _fire_context_updated_broadcast(self.get_file_prefix(), 'archive', chain_id)
            logger.info(f'链上下文已归档终结: {chain_id} / {self.get_file_prefix()}')
            self._cleanup_old_chain_dirs()
            return
        self._archive_unlinked()

    @handle_errors()
    def _archive_unlinked(self):
        """链外归档：轮转 unlinked 常驻文件（唯一保留重命名的场景）。"""
        self._session_history = []
        target = self.history_file
        if not target.exists():
            return
        try:
            with open(target, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = None
        if not history:
            return
        unlinked_dir = self.chains_root / 'unlinked'
        timestamp = datetime.now().strftime('%H%M%S')
        archive_file = unlinked_dir / f'{self.get_file_prefix()}_{timestamp}.json'
        seq = 1
        while archive_file.exists():
            seq += 1
            archive_file = unlinked_dir / f'{self.get_file_prefix()}_{timestamp}_{seq}.json'
        with open(archive_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        with open(target, 'w', encoding='utf-8') as f:
            json.dump([], f)
        logger.info(f'链外上下文已轮转归档: {archive_file.name}（共 {len(history)} 条消息）')
        _fire_context_updated_broadcast(self.get_file_prefix(), 'archive', None)

    @handle_errors()
    def _cleanup_old_chain_dirs(self):
        """清理旧链文件夹：保留最近 MAX_CHAIN_DIRS 个（进行中的链永不清理）。"""
        if not self.chains_root.exists():
            return
        chain_dirs = [d for d in self.chains_root.iterdir()
                      if d.is_dir() and d.name != 'unlinked']
        chain_dirs = [d for d in chain_dirs if d.name not in _active_chains]
        chain_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in chain_dirs[MAX_CHAIN_DIRS:]:
            shutil.rmtree(d, ignore_errors=True)
            logger.info(f'已删除旧链文件夹: {d.name}')

    # --------------------------------------------------------
    # 读写
    # --------------------------------------------------------

    @handle_errors()
    def _ensure_file_exists(self):
        """确保链外常驻文件存在"""
        if not self.history_file.exists():
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump([], f)

    @handle_errors()
    def append(self, message: dict):
        """保存历史记录：内存会话历史追加 + 磁盘展示文件累积落盘。

        链内写入落位到本次唤醒的独立文件 {prefix}_{HHMMSS}.json（写入即落位,
        永不重命名）；同时把链标记为进行中。LLM 上下文只看内存会话历史。
        """
        self._session_history.append(message)
        target = self._current_target_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with open(target, 'r', encoding='utf-8') as f:
                history = json.load(f)
        else:
            history = []
        history.append(message)
        temp_file = target.with_suffix('.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        self._atomic_replace(temp_file, target)
        # 链内写入: 登记 current_chain_id + 进行中链注册表
        from utils.chain_context import get_current_chain_id
        chain_id = get_current_chain_id()
        if chain_id:
            self.current_chain_id = chain_id
            _active_chains.add(chain_id)
        _fire_context_updated_broadcast(self.get_file_prefix(), 'append',
                                        chain_id if chain_id else None)
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
        """获取本次唤醒的会话历史（内存副本，不含之前唤醒的磁盘累积内容）"""
        return list(self._session_history)

    @handle_errors()
    def clear(self):
        """清空历史：重置内存会话历史，并将当前目标文件重置为空列表"""
        self._session_history = []
        target = self._current_target_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            json.dump([], f)
        logger.warning('历史记录已清空')
