"""灵光一现记事本：事件链级缓存文档 + 真正结束时以标题命名落库。

生命周期（挂钩点见 event_chain.py / pfca_tools.terminate）：
- 事件链开启（start_new_event_chain）→ begin_chain：创建该链的缓存文档（缓存态，不落盘）
- 链期间 conversation 调 record_to_notepad → append_note：记录进缓存文档
- PFCA terminate 第二次调用（真正结束）→ finalize_chain：以本次事件标题命名落盘持久化
- 事件链收尾（end_event_chain）→ discard_chain：丢弃未落库的残留缓存
  （max_rounds 熔断 / 强停等未经真正结束的路径不持久化）

反思消费接口（2026-09-15 新增，供 reflection 会话使用）：
- create_document：反思产出直接独立落库（不经过事件链缓存），标题自动生成
  （<落库时间>的反思留档），落库后立即进入活动池（get_all / render_manual 可见）；
  整篇字符数（标题 + 全部条目 + 总结）硬性少于 REFLECTION_NOTE_MAX_CHARS，
  超出自动截断不驳回
- archive_all：消费完成后将全部已落库笔记按落库时间（finalize_time 的月份）
  移入 notes/archive/<YYYY-MM>/ 归档文件夹，退出活动池；
  未转化为实际信息的笔记随归档视为自动丢弃（内容仍保留在归档文件中）

特殊储存槽位：
- 「任务进度相关」笔记双写到事件链缓存与任务槽位；普通笔记不再接受「事件见闻」类别。
- 见闻录由 Reflection 经 record_observation 独立写入，不再自动复制事件见闻笔记。
- 槽位独立于事件链文档与归档：archive_all 只 glob NOTES_DIR 顶层 *.json 并归档
  普通笔记，槽位位于 NOTES_DIR/slots/ 子目录，天然不受影响（反思整理不触碰槽位）；
- clear_slot 为显式清理接口；Quest 不再自动清空任务槽位。
- 见闻录槽位无自动清理，长期累积，保留观察到的现象及其依据。

约定：
- 全异步：文件 IO 通过 asyncio.to_thread 执行；纯内存生命周期操作为同步接口
  （由 event_chain 的同步生命周期方法直接调用，无 IO）。
- 重大关键方法失败不吞错：写盘/读盘 IO 异常直接上抛崩溃阻断；无活跃链文档等
  状态异常 raise RuntimeError 上抛崩溃阻断，防止错误扩大蔓延。
"""

from paths import data_root
import asyncio
import json
import os
import re
from uuid import uuid4
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger
from brain.hc.database.common import generate_timestamp_id

logger = get_logger('ShortlightNotepad', "INFO")

NOTES_DIR = data_root() / "brain" / "hc" / "shortlight" / "notes"

# 归档根目录名（NOTES_DIR 下的子目录，归档笔记按落库时间分月存放）。
# get_all 只 glob NOTES_DIR 顶层 *.json（非递归），归档子目录天然退出活动池。
ARCHIVE_DIR_NAME = "archive"

# 笔记类别（record_to_notepad 的 category 取值）：LLM 记录时必选其一。
NOTE_CATEGORY_TASK = "任务进度相关"
NOTE_CATEGORY_EVENT = "事件见闻"
# EVENT 保留用于历史数据识别及独立见闻录条目，不再属于普通笔记可写类别。
NOTE_CATEGORY_SELF_FIX = "自我修正"
NOTE_CATEGORY_QUOTE = "可借鉴的对方发言"
NOTE_CATEGORIES = [
    NOTE_CATEGORY_TASK,
    NOTE_CATEGORY_SELF_FIX,
    NOTE_CATEGORY_QUOTE,
]

# 特殊储存槽位：任务笔记双写，见闻录由反思独立写入。
# 槽位存于 NOTES_DIR/slots/<槽位名>.json，不受 archive_all
# 的顶层 glob 影响（反思整理只清普通笔记，不触碰槽位）。
SLOT_DIR_NAME = "slots"
SLOT_EVENT = "见闻录"
SLOT_TASK = "任务相关笔记"
SLOT_NAMES = [SLOT_EVENT, SLOT_TASK]
# 类别 → 槽位映射（仅任务笔记双写，其余类别只进普通笔记）
CATEGORY_TO_SLOT = {
    NOTE_CATEGORY_TASK: SLOT_TASK,
}

# list_notes 的可选范围：普通笔记（活动池）/ 见闻录 / 任务相关笔记
SCOPE_NORMAL = "普通笔记"
SCOPES = [SCOPE_NORMAL, SLOT_EVENT, SLOT_TASK]

# Windows 文件名非法字符 + 控制字符
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(title: str) -> str:
    """将标题清洗为合法文件名（非法字符替换为 _，去除首尾空白与末尾点）。"""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", title).strip().rstrip(".")
    return cleaned[:100]


class ShortlightNotepad:
    """灵光一现记事本。"""

    # 单条记录文本长度上限（字符）：记原则不记叙事，超限抛 ValueError
    # 走工具层 LLM 自纠通道（压缩重记）；类别标签由 category 字段承载
    # 2026-09-19 由 40 放宽到 60，允许带较短原话引用
    NOTE_TEXT_MAX_CHARS = 60

    # 反思笔记整篇字符上限：反思系统写的笔记（create_document 落库）整篇
    # 字符数（标题 + 全部条目 + 总结合计）必须少于该值（2026-09-23 新增），
    # 超限不驳回，按 标题→条目→总结 优先级自动截断；容量统计
    # count_unprocessed_chars 不排除反思笔记，短笔记对触发阈值影响可忽略
    REFLECTION_NOTE_MAX_CHARS = 150

    def __init__(self) -> None:
        self._notes_dir = NOTES_DIR
        self._notes_dir.mkdir(parents=True, exist_ok=True)
        # 特殊槽位目录（NOTES_DIR 子目录，archive_all 的顶层 glob 不会命中）
        self._slots_dir = NOTES_DIR / SLOT_DIR_NAME
        self._slots_dir.mkdir(parents=True, exist_ok=True)
        self._slot_write_lock = asyncio.Lock()
        # 当前事件链的缓存文档（缓存态，真正结束时才落盘）
        self._current: Optional[Dict[str, Any]] = None

    # ================= 事件链生命周期挂钩（纯内存，同步） =================

    def begin_chain(self, chain_id: str, trigger_text: str = "") -> None:
        """事件链开启：创建该链的缓存文档，并自动记录触发原因
        （truck 向 PFCA 注入的第一轮消息）。

        上一条链未收尾即开启新链属状态异常，直接崩溃阻断。
        """
        if self._current is not None:
            raise RuntimeError(
                f"记事本缓存文档未收尾即开启新链（残留 chain_id={self._current['chain_id']}），状态异常"
            )
        self._current = {
            "chain_id": chain_id,
            "trigger": (trigger_text or "").strip(),
            "begin_time": datetime.now().isoformat(timespec="milliseconds"),
            "notes": [],
        }
        logger.debug(f"记事本缓存文档已创建: {chain_id}")

    async def append_note(
        self,
        text: str,
        source: str,
        category: str,
        session_id: str = "",
        session_name: str = "",
    ) -> str:
        """写入一条记录进当前缓存文档，返回 note_id。

        category 为该条记录的类别标签（三类之一，见 NOTE_CATEGORIES）。
        无活跃链文档属状态异常（conversation 只会运行在事件链内），直接崩溃阻断；
        空文本/超长（> NOTE_TEXT_MAX_CHARS）属输入校验错误，抛 ValueError（走工具层 LLM 自纠通道）。

        仅「任务进度相关」双写任务槽位，其余类别只进缓存文档。
        见闻录须由反思独立记录。槽位写入 IO 失败直接上抛崩溃阻断。
        """
        if self._current is None:
            raise RuntimeError("无活跃事件链的记事本缓存文档，无法记录")
        cleaned_text = (text or "").strip()
        if not cleaned_text:
            raise ValueError("text 不能为空。")
        if len(cleaned_text) > self.NOTE_TEXT_MAX_CHARS:
            raise ValueError(
                f"text 超长（{len(cleaned_text)} 字 > 上限 {self.NOTE_TEXT_MAX_CHARS} 字）。"
                "只记提炼后的一句话（对方言论原话短引 / 要求标准 / 好恶 / 反应模式），"
                "不记事件经过与对话复述。"
            )
        cleaned_category = (category or "").strip()
        if cleaned_category not in NOTE_CATEGORIES:
            raise ValueError(
                f"category 非法（{cleaned_category!r}），必须从以下类别中选择之一："
                f"{'、'.join(NOTE_CATEGORIES)}。"
            )
        note_id = generate_timestamp_id("Note")
        note = {
            "note_id": note_id,
            "text": cleaned_text,
            "source": source,
            "category": cleaned_category,
            "session_id": session_id,
            "session_name": session_name,
            "record_time": datetime.now().isoformat(timespec="milliseconds"),
        }
        self._current["notes"].append(note)
        # 仅任务进度相关双写；见闻录由反思专属接口写入。
        slot = CATEGORY_TO_SLOT.get(cleaned_category)
        if slot is not None:
            async with self._slot_write_lock:
                await asyncio.to_thread(self._append_slot_note_sync, slot, note)
        return note_id

    async def record_observation(self, text: str, source: str = "reflection") -> str:
        """反思独立记录见闻，不要求活跃事件链，不占普通笔记预算。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 必须是一段非空的见闻文字。")
        if source not in ("reflection", "human"):
            raise ValueError("见闻来源必须为 reflection 或 human")
        note_id = "Note_" + uuid4().hex
        note = {
            "note_id": note_id,
            "text": text.strip(),
            "source": source,
            "category": NOTE_CATEGORY_EVENT,
            "session_id": "",
            "session_name": "",
            "record_time": datetime.now().isoformat(timespec="milliseconds"),
        }
        async with self._slot_write_lock:
            await asyncio.to_thread(self._append_slot_note_sync, SLOT_EVENT, note)
        return note_id

    async def update_observation(self, note_id: str, text: str) -> Optional[Dict[str, Any]]:
        """人从记忆页修订独立见闻；保留原始来源和记录时间。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 必须是一段非空的见闻文字。")
        async with self._slot_write_lock:
            file_path = self._slot_path(SLOT_EVENT)
            if not file_path.exists():
                return None
            payload = (await asyncio.to_thread(self._read_documents, [file_path]))[0]
            note = next((item for item in payload.get("notes", []) if item.get("note_id") == note_id), None)
            if note is None:
                return None
            note["text"] = text.strip()
            note["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
            await asyncio.to_thread(self._write_slot_sync, file_path, payload)
            return dict(note)

    async def delete_observation(self, note_id: str) -> bool:
        """删除指定独立见闻，不触碰普通笔记与任务槽位。"""
        async with self._slot_write_lock:
            file_path = self._slot_path(SLOT_EVENT)
            if not file_path.exists():
                return False
            payload = (await asyncio.to_thread(self._read_documents, [file_path]))[0]
            notes = payload.get("notes", [])
            kept = [item for item in notes if item.get("note_id") != note_id]
            if len(notes) == len(kept):
                return False
            payload["notes"] = kept
            await asyncio.to_thread(self._write_slot_sync, file_path, payload)
            return True

    def discard_chain(self) -> None:
        """事件链收尾：丢弃未落库的残留缓存（未真正结束路径不持久化）。"""
        if self._current is not None:
            dropped = len(self._current["notes"])
            if dropped:
                logger.warning(
                    f"事件链 {self._current['chain_id']} 收尾：记事本未经真正结束，"
                    f"缓存不落库，已丢弃 {dropped} 条记录"
                )
            self._current = None

    async def finalize_chain(
        self,
        title: str,
        final_evaluation: Optional[str] = None,
        summary: str = "",
    ) -> str:
        """真正结束（terminate 第二次确认调用）：以本次事件标题命名落盘，返回文件名。

        final_evaluation 为 PFCA 对本事件链的最终评价（词表名，来自 FinalEvaluationAct）；
        summary 为 PFCA 的本次会话总结（完整前因后果、自我感受），随文档一并落库。
        标题缺失/清洗后为空时以 chain_id 兜底命名（素材不丢）。
        """
        if self._current is None:
            raise RuntimeError("无活跃事件链的记事本缓存文档，无法落库")
        doc = self._current
        self._current = None
        cleaned_title = (title or "").strip()
        base_name = _safe_filename(cleaned_title) or doc["chain_id"]
        payload = {
            "title": cleaned_title or doc["chain_id"],
            "chain_id": doc["chain_id"],
            "trigger": doc["trigger"],
            "begin_time": doc["begin_time"],
            "finalize_time": datetime.now().isoformat(timespec="milliseconds"),
            "final_evaluation": final_evaluation,
            "summary": (summary or "").strip(),
            "notes": doc["notes"],
        }
        name = await asyncio.to_thread(self._write_document, base_name, payload)
        logger.info(f"记事本文档已落库: {name}.json（{len(payload['notes'])} 条记录）")
        return name

    def _write_document(self, base_name: str, payload: Dict[str, Any]) -> str:
        """同步写盘。重名自动追加序号；IO 失败直接上抛，崩溃阻断。"""
        candidate = base_name
        suffix = 2
        while (self._notes_dir / f"{candidate}.json").exists():
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        with open(self._notes_dir / f"{candidate}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return candidate

    async def get_all(self) -> List[Dict[str, Any]]:
        """读取全部已落库文档（按文件名排序）。供未来反思系统消费。"""
        file_paths = sorted(self._notes_dir.glob("*.json"))
        return await asyncio.to_thread(self._read_documents, file_paths)

    async def get_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """按标题精确查询一篇已落库文档；重名时返回最新一份；不存在返回 None。"""
        docs = await self.get_all()
        for doc in reversed(docs):
            if doc.get("title") == (title or "").strip():
                return doc
        return None

    async def get_notes_by_category(self, category: str) -> List[Dict[str, Any]]:
        """聚合指定类别的全部笔记条目（当前链缓存 + 已落库），按记录时间正序返回。

        通用类别聚合接口：任务进度相关条目现在由 quest agent 直接读专用槽位
        （get_slot_notes），此接口保留供其他类别的跨文档聚合查询使用。

        Returns:
            条目列表（每项含 text / source / category / record_time 等字段）；
            无匹配返回空列表。
        """
        target = (category or "").strip()
        results: List[Dict[str, Any]] = []
        if self._current is not None:
            for note in self._current.get("notes") or []:
                if (note.get("category") or "").strip() == target:
                    results.append(note)
        for doc in await self.get_all():
            for note in doc.get("notes") or []:
                if (note.get("category") or "").strip() == target:
                    results.append(note)
        results.sort(key=lambda n: n.get("record_time", ""))
        return results

    def _read_documents(self, file_paths: List[Path]) -> List[Dict[str, Any]]:
        """同步读盘。IO/解析失败直接上抛，崩溃阻断。"""
        results: List[Dict[str, Any]] = []
        for file_path in file_paths:
            with open(file_path, "r", encoding="utf-8") as f:
                results.append(json.load(f))
        return results

    # ============================================================
    # 特殊储存槽位（见闻录 / 任务相关笔记）：独立于事件链文档与归档
    # ============================================================

    def _slot_path(self, slot: str) -> Path:
        """校验槽位名并返回其文件路径。非法槽位名属编码错误，直接崩溃阻断。"""
        cleaned = (slot or "").strip()
        if cleaned not in SLOT_NAMES:
            raise ValueError(
                f"槽位名非法（{cleaned!r}），必须是以下之一：{'、'.join(SLOT_NAMES)}。"
            )
        return self._slots_dir / f"{_safe_filename(cleaned)}.json"

    def _append_slot_note_sync(self, slot: str, note: Dict[str, Any]) -> None:
        """同步向槽位文件追加一条笔记（读-改-写整文件）。

        槽位文件为独立 JSON（{"slot", "notes": [...]}），与事件链文档解耦；
        读/写 IO 失败直接上抛崩溃阻断。
        """
        file_path = self._slot_path(slot)
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = {"slot": slot, "notes": []}
        payload.setdefault("notes", []).append(note)
        self._write_slot_sync(file_path, payload)

    async def get_slot_notes(self, slot: str) -> List[Dict[str, Any]]:
        """读取指定槽位的全部笔记条目，按记录时间正序返回；无槽位文件返回空列表。"""
        file_path = self._slot_path(slot)
        if not file_path.exists():
            return []
        payload = await asyncio.to_thread(self._read_documents, [file_path])
        notes = payload[0].get("notes") or []
        return sorted(notes, key=lambda n: n.get("record_time", ""))

    async def clear_slot(self, slot: str) -> int:
        """清空指定槽位内容，返回被丢弃的条目数；无槽位文件返回 0。

        任务 agent 处理完毕后调用（丢弃任务相关笔记槽位）；只清槽位，
        不影响普通笔记中同条目的副本（仍可由反思系统消费）。
        """
        file_path = self._slot_path(slot)
        if not file_path.exists():
            return 0
        payload = (await asyncio.to_thread(self._read_documents, [file_path]))[0]
        dropped = len(payload.get("notes") or [])
        payload["notes"] = []
        await asyncio.to_thread(self._write_slot_sync, file_path, payload)
        logger.info(f"槽位「{slot}」已清空 {dropped} 条笔记")
        return dropped

    def _write_slot_sync(self, file_path: Path, payload: Dict[str, Any]) -> None:
        """同步写回槽位文件。IO 失败直接上抛，崩溃阻断。"""
        temporary = file_path.with_name(file_path.name + "." + uuid4().hex + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(temporary, file_path)
        finally:
            temporary.unlink(missing_ok=True)

    # ============================================================
    # 反思消费接口（reflection 会话专用：独立落库 + 按时间归档）
    # ============================================================

    async def create_document(
        self,
        notes: List[str],
        summary: str = "",
        trigger: str = "",
    ) -> str:
        """落库一篇独立的反思笔记文档（反思产出专用），标题自动生成。

        与事件链生命周期（begin_chain/append_note/finalize_chain）完全解耦：
        reflection 消费旧笔记后，把本轮提炼条目一次性落库为新笔记，
        落库后立即进入活动池（get_all / render_manual 可见）。

        标题自动生成为「<落库时间>的反思留档」，外部无需提供。
        整篇字符数（标题 + 全部条目 + 总结合计）硬性少于
        REFLECTION_NOTE_MAX_CHARS：超出限制不驳回，按 标题 → 条目（依序
        填充）→ 总结 的优先级自动截断（截断发生时记 warning 日志）。
        notes 为空或清洗后无有效条目抛 ValueError（走工具层 LLM 自纠通道）。

        Args:
            notes: 提炼条目文本列表（依序成为条目，source 固定 "reflection"，
                超出预算部分自动截断/丢弃）。
            summary: 可选总结，随文档一并落库（排在截断优先级最后）。
            trigger: 可选。本篇笔记的来由（如反思触发原因）。

        Returns:
            落库文件名（不含扩展名，重名自动追加 _2 序号）。
        """
        items: List[str] = []
        for text in notes or []:
            cleaned = (text or "").strip()
            if cleaned:
                items.append(cleaned)
        if not items:
            raise ValueError("items 条目列表不能为空（反思留档内容即提炼条目）。")

        full_title = f"{datetime.now().strftime('%Y-%m-%d %H:%M')}的反思留档"
        # 整篇预算：须少于 REFLECTION_NOTE_MAX_CHARS，超出自动截断不驳回
        budget = self.REFLECTION_NOTE_MAX_CHARS - 1
        title = full_title[:budget]
        remain = budget - len(title)
        entries: List[Dict[str, Any]] = []
        for text in items:
            if remain <= 0:
                break
            taken = text[:remain]
            remain -= len(taken)
            entries.append({
                "note_id": generate_timestamp_id("Note"),
                "text": taken,
                "source": "reflection",
                "category": NOTE_CATEGORY_SELF_FIX,
                "session_id": "",
                "session_name": "",
                # 反思条目不落条目级时间（全部同属 finalize 时刻，逐条展示冗余无意义；
                # 文档级 begin_time/finalize_time 已承载时间信息）
                "record_time": "",
            })
        cleaned_summary = (summary or "").strip()

        # 截断观测：整篇超限时记 warning，便于追查反思产出是否过长
        original_total = (
            len(full_title) + sum(len(t) for t in items) + len(cleaned_summary)
        )
        cleaned_summary = cleaned_summary[:max(remain, 0)]
        final_total = (
            len(title)
            + sum(len(e["text"]) for e in entries)
            + len(cleaned_summary)
        )
        if final_total < original_total:
            logger.warning(
                f"反思笔记整篇超限已自动截断：{original_total} 字 -> "
                f"{final_total} 字（上限 {self.REFLECTION_NOTE_MAX_CHARS} 字）"
            )

        now = datetime.now().isoformat(timespec="milliseconds")
        payload = {
            "title": title,
            "chain_id": generate_timestamp_id("Chain"),
            "trigger": (trigger or "").strip(),
            "begin_time": now,
            "finalize_time": now,
            "final_evaluation": None,
            "summary": cleaned_summary,
            "notes": entries,
        }
        name = await asyncio.to_thread(self._write_document, _safe_filename(title), payload)
        logger.info(f"反思新笔记已落库: {name}.json（{len(entries)} 条记录）")
        return name

    async def archive_all(self) -> List[Dict[str, str]]:
        """将全部已落库笔记按落库时间（finalize_time 的月份）归档到
        notes/archive/<YYYY-MM>/ 文件夹。

        反思消费笔记后的收尾动作：归档后笔记退出活动池（get_all / render_manual /
        get_by_title 不再可见），文件原样移动，内容全部保留——未转化为实际信息的
        笔记随归档视为自动丢弃。无已落库笔记时返回空列表。

        Returns:
            逐篇归档结果列表：[{"title", "file", "dest"}]，
            file 为原文件名，dest 为归档后的相对路径（相对 NOTES_DIR）。
        """
        file_paths = sorted(self._notes_dir.glob("*.json"))
        if not file_paths:
            return []
        docs = await asyncio.to_thread(self._read_documents, file_paths)
        return await asyncio.to_thread(self._archive_documents, file_paths, docs)

    def _archive_documents(
        self, file_paths: List[Path], docs: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """同步归档：逐篇推导月份目录并移动。

        finalize_time 缺失/非法属数据损坏，raise RuntimeError 崩溃阻断；
        移动 IO 失败直接上抛。
        """
        results: List[Dict[str, str]] = []
        for file_path, doc in zip(file_paths, docs):
            finalize_time = (doc.get("finalize_time") or "").strip()
            try:
                month = datetime.fromisoformat(finalize_time).strftime("%Y-%m")
            except ValueError as e:
                raise RuntimeError(
                    f"笔记 {file_path.name} 的 finalize_time 非法"
                    f"（{finalize_time!r}），无法按时间归档"
                ) from e
            dest_dir = self._notes_dir / ARCHIVE_DIR_NAME / month
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_name = self._move_document(file_path, dest_dir)
            results.append({
                "title": doc.get("title", ""),
                "file": file_path.name,
                "dest": f"{ARCHIVE_DIR_NAME}/{month}/{dest_name}",
            })
        logger.info(f"记事本已按时间归档 {len(results)} 篇笔记")
        return results

    def _move_document(self, file_path: Path, dest_dir: Path) -> str:
        """同步移动一篇笔记到归档目录，重名自动追加序号，返回归档后的文件名。"""
        base = file_path.stem
        candidate = base
        suffix = 2
        while (dest_dir / f"{candidate}.json").exists():
            candidate = f"{base}_{suffix}"
            suffix += 1
        dest = dest_dir / f"{candidate}.json"
        file_path.replace(dest)
        return f"{candidate}.json"

    # ============================================================
    # 呈现层：聚合渲染为「按标题分章的手册」（供 conversation / PFCA 注入上下文）
    # ============================================================

    # 每章呈现的最大条目数（新条目优先，旧条目挤出呈现层；存储不受影响）。
    # 实际呈现条数会被字符预算进一步压低（约 2-3 条/章）
    MANUAL_MAX_ENTRIES_PER_CHAPTER = 5
    # 手册默认字符预算（逐步拉取：从最新章往回逐章拉取，累计超预算即停止拉取）。
    # 调用方按需传 max_chars 覆盖（PFCA 传 1000，见 pfca_input_manager）
    MANUAL_MAX_CHARS = 300

    async def render_manual(self, max_chars: int | None = None) -> str:
        """把已落库文档按「从新到旧逐步拉取」渲染成「章节式手册」文本。

        拉取规则（字符预算驱动）：按落库时间从最新章往回逐章拉取，
        若拉取某章会导致累计字符数超过 max_chars，则停止继续拉取（该章不展示）；
        最新章保底完整保留（避免空手册）。每章条目新挤旧（最多
        MANUAL_MAX_ENTRIES_PER_CHAPTER 条）。多份同名标题文档合并为同一章。

        存储不受影响：只影响呈现层注入文本体积，防止注入过大干扰 LLM；
        细节溯源走 query_note 工具。空库/全部被限幅挤出时返回空串（注入方应跳过）。

        条目的 category 字段为类别标签（query_note 查详情可见，兼容历史类别），
        不参与手册路由——所有已落库条目全部入选渲染。

        Args:
            max_chars: 字符预算上限；None 用 MANUAL_MAX_CHARS 默认值。

        Returns:
            形如（段头【记事本手册】与其他首条条件段统一）：
                【记事本手册】
                记事本存在以下章节:
                帮用户排查报错
                └ 用户情绪低落时先共情再给建议
                哄用户睡觉
                └ 用户要求我下次主动点，我应该记住

                可以使用工具查询记事本某一章节的具体信息以查看细节。
        """
        if max_chars is None:
            max_chars = self.MANUAL_MAX_CHARS
        docs = await self.get_all()
        # title -> {"notes": 入选条目, "last_time": 该章最新落库时间}
        chapters: Dict[str, Dict[str, Any]] = {}
        for doc in docs:
            title = (doc.get("title") or "").strip()
            if not title:
                continue
            info = chapters.setdefault(title, {"notes": [], "last_time": ""})
            ftime = doc.get("finalize_time") or ""
            if ftime > info["last_time"]:
                info["last_time"] = ftime
            for note in doc.get("notes") or []:
                info["notes"].append(note)

        # 逐章去重 + 新挤旧，组装章文本（章头 + └ 条目行）
        rendered: List[tuple] = []  # (last_time, 章文本行列表)
        for title, info in chapters.items():
            seen: set = set()
            entries: List[str] = []
            for note in sorted(info["notes"], key=lambda n: n.get("record_time", "")):
                text = (note.get("text") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                entries.append(text)
            if not entries:
                continue
            lines = [title] + [f"└ {t}" for t in entries[-self.MANUAL_MAX_ENTRIES_PER_CHAPTER:]]
            rendered.append((info["last_time"], lines))
        if not rendered:
            return ""

        # 逐步拉取：升序稳定排序（同 finalize_time 毫秒撞值时保持落库文件序，
        # 语义即落库先后），从最新章往回逐章拉取，累计字符超 max_chars 即停止。
        rendered.sort(key=lambda r: r[0])
        kept: List[List[str]] = []
        total = 0
        for _, lines in reversed(rendered):
            size = sum(len(line) for line in lines)
            if kept and total + size > max_chars:
                break
            kept.append(lines)
            total += size
        if not kept:
            return ""

        # 展示顺序恢复为落库旧→新（章内条目本就时间正序）
        kept.reverse()
        lines_out: List[str] = ["【记事本手册】", "记事本存在以下章节:"]
        for chapter_lines in kept:
            lines_out.extend(chapter_lines)
        lines_out.append("")
        lines_out.append("可以使用工具查询记事本某一章节的具体信息以查看细节。")
        return "\n".join(lines_out)

    # ============================================================
    # 反思触发统计：未处理笔记字符数（活动池全量，不排除任何来源）
    # ============================================================

    async def count_unprocessed_chars(self) -> int:
        """统计未处理笔记（活动池全部已落库笔记）的总字符数。

        反思触发字符判断依据：活动池内全部笔记（不排除任何来源）的
        标题 + 全部条目文本的字符总数；反思系统自己写的笔记（create_document
        落库）受 REFLECTION_NOTE_MAX_CHARS（<150 字）硬约束，体积极小，
        对触发阈值影响可忽略。

        Returns:
            int: 未处理笔记的字符总数（活动池为空返回 0）。
        """
        docs = await self.get_all()
        total = 0
        for doc in docs:
            total += len((doc.get("title") or "").strip())
            for n in doc.get("notes") or []:
                total += len((n.get("text") or "").strip())
        return total


_instance: Optional[ShortlightNotepad] = None


def get_shortlight_notepad() -> ShortlightNotepad:
    """获取记事本单例。"""
    global _instance
    if _instance is None:
        _instance = ShortlightNotepad()
    return _instance
