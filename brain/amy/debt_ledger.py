#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 欠账台账（系统级共享常驻状态）

设计（2026-09-29 拍板）：
- **明细是主体**：每条欠账 = {id, title, kind, score, create_time}；
  标量只是聚合视图（合计点数 = sum(score)），不单独存储，避免两处数值分叉。
- 记账：apply_final_evaluation 结算时 delta < 0（-10 / -20）→ 追加一条明细。
- 销账（分数值口径）：delta > 0（当前仅 +20，后续若新增 +10 自动生效）作为**额度**，
  从最旧欠账开始抵扣；额度可拆分跨笔冲抵，超额部分丢弃（表扬不预存）。
  +10 可抵一笔 -10；+20 可抵一笔 -20，也可抵两笔 -10。
- **永不过期**：欠账只由正向评价销账，不做时间清理。
- 系统级共享：模块级单例，任何模块 get_debt_ledger() 拿到的是同一对象、同一份 JSON，
  不存在各人格/各 agent 各持一份的情况。
- 持久化：brain/amy/persistence/debt_ledger.json（该目录已被 .gitignore 忽略）。
- 单例访问器：get_debt_ledger() / reset_debt_ledger()
  （模块级工厂，普通类 + 全局 _instance；禁止类内 __new__ 单例模板），
  与 feedback_state.py 保持一致。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from brain.amy.constants import generate_timestamp_id
from utils.error_handling import handle_errors
from utils.logger import get_logger

logger = get_logger('amy_debt_ledger', 'INFO')


@dataclass
class DebtEntry:
    """一笔未偿欠账（负债点数为负，与 feedback_delta 同号）。"""

    id: str
    title: str
    kind: str        # 记账时的评价词（FinalEvaluationAct 词表项）
    score: float     # 负债点数（负数）
    create_time: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "score": self.score,
            "create_time": self.create_time,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DebtEntry":
        return cls(
            id=data["id"],
            title=data["title"],
            kind=data["kind"],
            score=float(data["score"]),
            create_time=int(data["create_time"]),
        )


class DebtLedger:
    """欠账台账（普通类，单例由模块级 get_debt_ledger 持有）。

    内部状态：
    - _entries: List[DebtEntry]  按 create_time 升序（append 顺序即时间顺序）
    """

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = Path(__file__).resolve().parent / "persistence"
        persistence_dir.mkdir(exist_ok=True)
        self.persistence_file = persistence_dir / "debt_ledger.json"
        self._entries: List[DebtEntry] = []
        self.load_from_file()

    # ===== 记账 / 销账 =====

    def record_debt(self, title: str, kind: str, score: float) -> DebtEntry:
        """记一笔欠账（score 必须为负）；返回新条目。落盘由门面统一 save_to_file。"""
        if score >= 0:
            raise ValueError(f"欠账点数必须为负，收到: {score}")
        if not title or not title.strip():
            raise ValueError("欠账必须提供事件标题 title（≤15 字）")
        entry = DebtEntry(
            id=generate_timestamp_id(),
            title=title.strip(),
            kind=kind,
            score=float(score),
            create_time=int(time.time()),
        )
        self._entries.append(entry)
        return entry

    def settle_with_credit(self, credit: float) -> float:
        """用一笔正向评价额度冲抵欠账（从最旧开始，可拆分跨笔），返回实际抵消点数。

        超额部分丢弃（表扬不预存）；credit <= 0 时不做任何事，返回 0.0。
        """
        if credit <= 0:
            return 0.0
        remaining = float(credit)
        settled = 0.0
        survivors: List[DebtEntry] = []
        for entry in self._entries:
            if remaining <= 0:
                survivors.append(entry)
                continue
            take = min(remaining, -entry.score)
            entry.score += take
            remaining -= take
            settled += take
            if entry.score < 0:
                survivors.append(entry)
        self._entries = survivors
        return settled

    # ===== 读出 =====

    def list_debts(self) -> List[DebtEntry]:
        """按时间升序列出全部未偿欠账（明细是主体，动作只能指向这里的 title）。"""
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def total(self) -> float:
        """聚合视图：未偿点数合计（负数，0 表示无欠账）。"""
        return float(sum(entry.score for entry in self._entries))

    # ===== 持久化 =====

    @handle_errors()
    def load_from_file(self) -> None:
        """从 debt_ledger.json 载入 {entries}；首启无文件则为空台账。"""
        if not self.persistence_file.exists():
            return
        with open(self.persistence_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self._entries = [DebtEntry.from_dict(item) for item in data.get("entries", [])]
        logger.info(f"Loaded debt_ledger from persistence: {self.count()} 笔，合计 {self.total()}")

    @handle_errors()
    def save_to_file(self) -> None:
        """持久化 {entries} 到 debt_ledger.json。"""
        data = {"entries": [entry.to_dict() for entry in self._entries]}
        with open(self.persistence_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


_debt_ledger: Optional[DebtLedger] = None


def get_debt_ledger(persistence_dir: Optional[Path] = None) -> DebtLedger:
    """获取欠账台账单例（系统级共享，首次创建时载入持久化）。

    注意：persistence_dir 仅在首次创建时生效；换目录走 reset_debt_ledger()
    后重建（web 预设 / 测试隔离流程依赖该语义）。
    """
    global _debt_ledger
    if _debt_ledger is None:
        _debt_ledger = DebtLedger(persistence_dir)
    return _debt_ledger


def reset_debt_ledger() -> None:
    """清除单例（测试/预设环境用）。下次 get_debt_ledger 重建。"""
    global _debt_ledger
    _debt_ledger = None
