# -*- coding: utf-8 -*-
"""内心人格注册表 - LLM 多重人格机制的数据层。

设计记录：docs/已完成.md（inner_persona 人格机制）

理念：LLM 拥有多重人格，就像人类有多种身份。每个人格是一份预设的
内核提示词（kernel_prompt），召唤时注入 PersonaAgent 的 system prompt，
让 LLM 从该人格的立场与思考风格审视问题——类似脑内的天使与恶魔。

存储（2026-09-28 起改为文件化）：每个人格一个 JSON 文件，存于本目录
（brain/base/persona/*.json），文件含
persona_name / scene / kernel_prompt / model_level。
模块导入时同步加载全部 *.json 为注册表 _PERSONAS；运行时改动定义
经 persist_persona 异步回写（同步 IO 用 asyncio.to_thread 包装，
遵循项目全异步约定）。

kernel_prompt 是人格定义的唯一来源（2026-09-29 移除经验挂载链路）：
人格不再挂载经验 ID 列表——该链路既无运行时注入（人格 system prompt
只含 kernel_prompt），也无任何有效消费方，属纯死代码。人格需要遵循
某条经验时，直接写进 kernel_prompt；可检索的经验统一留在经验库，
由持有 query_bequest 的 agent 按需检索。

基本人格（2026-09-29）：与普通人格平行的一条特殊人格条目，固定
persona_name = "common"（存 common.json）。它按与其他人格完全相同的
方式被查询，但不参与内荐（list_roster_personas 排除它），也不能作为
会话人格使用。
拼装给会话的人格提示词 = 基本人格全文 + "\n" + 该人格 kernel_prompt
（基本人格在前），经 compose_persona_prompt 输出给 conversation /
social_chat 首条消息；人格自荐 Agent 仍只用 kernel_prompt，不拼基本人格。

分层与冻结（2026-10-01，方案一「common 降格为基本说话风格」）：
- 普通人格的 kernel_prompt 对 LLM 冻结（update_persona 硬拦，仅主人手改）；
  唯一可经工具修改的是基本人格 common 的 kernel_prompt——其语义收窄为
  「基本说话风格」：说人话式的普适性要求，旨在让 LLM 说话自然，不做
  逐字逐词的细则管制，硬预算 COMMON_STYLE_BUDGET 字符，超限拒绝。
- 运行时覆盖写入前自动留档到 history/ 子目录（同名带时间戳），可回滚。
  history/ 不参与注册表加载（glob 仅取目录直属 *.json）。

人格文件读取 / 解析失败属重大关键路径失败，直接上抛崩溃阻断，
禁止 try-except 吞掉继续（user_rules）。唯一例外是人格目录为空
（全新数据目录 / 首次部署）：此时自动播种一份空的基本人格模板
（_seed_personas_if_empty），避免注册表加载即崩；该例外不覆盖
"文件已存在但内容坏"的情况，后者照旧崩溃。
"""

from paths import data_root
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from utils.logger import get_logger

logger = get_logger('persona_registry', "INFO")

# 人格文件目录：本模块同级目录，每个人格一个 <persona_id>.json
PERSONA_DIR = data_root() / "brain" / "base" / "persona"
PERSONA_DIR.mkdir(parents=True, exist_ok=True)

# 基本人格的保留 persona_name（普通人格条目 + 此条，见模块 docstring）
COMMON_PERSONA_NAME = "common"

# 基本人格 kernel_prompt（基本说话风格）的硬预算（字符数）：
# 超过即拒绝写入（update_persona 工具层强制），迫使做取舍而非无限累积。
COMMON_STYLE_BUDGET = 300

# 运行时改动留档目录（PERSONA_DIR/history/，同步 IO 在 to_thread 中执行）
PERSONA_HISTORY_DIR = PERSONA_DIR / "history"


@dataclass
class PersonaDef:
    """单个人格的定义。

    Attributes:
        persona_name: 人格名，同时是注册表键与 LLM 工具入参（如 "task"）。
        scene: 适配场景一句话说明（列表展示用，即该人格的梗概）。
        kernel_prompt: 人格内核提示词。普通人格定义该人格的立场、动机与
            思考风格（对 LLM 冻结）；基本人格收窄为「基本说话风格」
            （说人话式普适要求，LLM 唯一可改，硬预算 COMMON_STYLE_BUDGET）。
        model_level: 该人格使用的 LLM 模型等级（call_llm 的 level）：
            persona = 内心人格专属接入点（默认智谱 GLM）；flash = DeepSeek 主力接入点。
    """

    persona_name: str
    scene: str
    kernel_prompt: str
    model_level: str = "persona"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可落盘的 JSON 字典（字段与人格文件格式一致）。"""
        return {
            "persona_name": self.persona_name,
            "scene": self.scene,
            "kernel_prompt": self.kernel_prompt,
            "model_level": self.model_level,
        }


# ==================== 加载 ====================

def _persona_path(persona_name: str) -> Path:
    """人格文件路径：<PERSONA_DIR>/<persona_name>.json。"""
    return PERSONA_DIR / f"{persona_name}.json"


def _write_persona_file(persona: PersonaDef) -> None:
    """同步落盘单个人格 JSON。IO 失败直接上抛，崩溃阻断。"""
    import os
    import tempfile
    fd, name = tempfile.mkstemp(dir=PERSONA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(persona.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(name, _persona_path(persona.persona_name))
    finally:
        if os.path.exists(name):
            os.unlink(name)


# 全新数据目录（打包发布 / 首次部署）不携带人格文件：自动播种一份空的基本人格
# 模板，正文交用户自行填写（2026-10-15 决定：初始只带一条空 common，其余人格
# 由用户按需自建）。kernel_prompt 留空是安全的（compose_persona_prompt 对空内容
# 有兜底）；scene 给占位是因为它会进 render_persona_roster 的人格清单。
SEED_PERSONA_TEMPLATE = PersonaDef(
    persona_name=COMMON_PERSONA_NAME,
    scene="基本说话风格（待填写）",
    kernel_prompt="",
)


def _seed_personas_if_empty() -> None:
    """人格目录内无任何 *.json 时，播种基本人格模板。

    只兜底「目录为空」这一种情况；目录内已有文件（哪怕内容坏）不在此处理，
    仍走 _load_personas 的崩溃阻断（缺字段 / JSON 坏照旧上抛，user_rules）。
    """
    if any(PERSONA_DIR.glob("*.json")):
        return
    _write_persona_file(SEED_PERSONA_TEMPLATE)
    logger.info(f"人格目录为空，已播种基本人格模板: {_persona_path(COMMON_PERSONA_NAME)}")


def _load_personas() -> Dict[str, PersonaDef]:
    """从本目录全部 *.json 加载人格（模块导入时同步执行）。

    目录为空时先播种基本人格模板（见 _seed_personas_if_empty）；
    任一文件缺失字段 / JSON 解析失败直接上抛崩溃阻断（user_rules）。
    """
    _seed_personas_if_empty()
    personas: Dict[str, PersonaDef] = {}
    for path in sorted(PERSONA_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        persona = PersonaDef(
            persona_name=data["persona_name"],
            scene=data["scene"],
            kernel_prompt=data["kernel_prompt"],
            model_level=data.get("model_level", "persona"),
        )
        personas[persona.persona_name] = persona
    if not personas:
        raise RuntimeError(f"人格目录无任何人格文件（*.json）: {PERSONA_DIR}")
    if COMMON_PERSONA_NAME not in personas:
        raise RuntimeError(
            f"人格目录缺少基本人格条目（{COMMON_PERSONA_NAME}.json）: {PERSONA_DIR}"
        )
    return personas


# 模块级注册表：persona_name -> PersonaDef（导入即从人格文件加载）
_PERSONAS: Dict[str, PersonaDef] = _load_personas()


def get_base_persona() -> PersonaDef:
    """返回基本人格条目（persona_name = "common"）。"""
    return _PERSONAS[COMMON_PERSONA_NAME]


def compose_persona_prompt(persona: PersonaDef) -> str:
    """拼装实际给会话的人格提示词：基本人格在前，该人格 kernel_prompt 在后。

    基本人格条目自身（persona_name = "common"）直接返回自己的 kernel_prompt，
    不重复拼接；基本人格为空时直接返回人格内核（不留多余空行）。
    """
    if persona.persona_name == COMMON_PERSONA_NAME:
        return persona.kernel_prompt
    base = get_base_persona().kernel_prompt
    if not base.strip():
        return persona.kernel_prompt
    return f"{base}\n{persona.kernel_prompt}"


# 缺省人格：调用方（conversation_agent / social_chat_agent）未指定 persona_id 时使用。
# 2026-09-29 起人格改为逐会话显式指定：PFCA 不再持有 / 注入"当前人格"，
# 由工具层把 persona_id 透传到对应会话，会话首条消息注入该人格内核。
# 2026-10-15：全新数据目录只播种基本人格（_seed_personas_if_empty），
# normal 不存在时缺省回落到基本人格 common，避免默认人格 KeyError 崩溃。
DEFAULT_PERSONA_MODE = "normal" if "normal" in _PERSONAS else COMMON_PERSONA_NAME


def register_persona(persona: PersonaDef) -> None:
    """注册（或覆盖）一个人格到内存注册表（同 persona_name 覆盖旧定义）。

    仅改内存，不落盘：如需持久化到人格文件，另调 persist_persona（异步）。
    """
    _PERSONAS[persona.persona_name] = persona
    logger.info(f"人格已注册: {persona.persona_name}")


def persona_exists(persona_name: str) -> bool:
    """判断人格是否已注册（供工具层对非法入参走 LLM 自纠通道）。"""
    return persona_name in _PERSONAS


def get_persona(persona_name: str) -> PersonaDef:
    """按 persona_name 获取人格定义。

    人格不存在属重大关键路径失败，直接抛 KeyError 崩溃阻断，
    禁止 try-except 吞掉继续（user_rules）。
    """
    return _PERSONAS[persona_name]


def list_personas() -> List[PersonaDef]:
    """列出全部已注册人格（含基本人格条目，注册表插入顺序）。"""
    return list(_PERSONAS.values())


def list_roster_personas() -> List[PersonaDef]:
    """列出参与内荐 / 可作会话人格的人格（排除基本人格条目）。"""
    return [p for p in _PERSONAS.values() if p.persona_name != COMMON_PERSONA_NAME]


def render_persona_roster() -> str:
    """渲染人格清单文本（常备描述，供需要让 LLM 知晓可选人格的提示词嵌入）。

    只列参与内荐 / 可作会话人格的人格（不含基本人格条目），
    每行「人格名：适配场景」。切换人格不影响身份归属，仅换一套应对手段。
    """
    lines = ["你具有以下人格。"]
    lines += [f"{p.persona_name}：{p.scene}" for p in list_roster_personas()]
    lines.append(
        "人格是一种应对特定方面事件的状态，切换人格仍然是你，"
        "只是切换了一套快速用于该类事件的经验集合与手段。"
        "你可以选择性的切换与使用他们。"
    )
    return "\n".join(lines)


# ==================== 持久化 ====================

async def persist_persona(persona: PersonaDef) -> None:
    """异步落盘单个人格（同步 IO 用 asyncio.to_thread 包装，项目全异步约定）。"""
    await asyncio.to_thread(_write_persona_file, persona)


def _snapshot_persona(persona_name: str) -> None:
    """运行时改动前留档：把当前人格文件复制到 history/<名>_<时间戳>.json。

    供回滚溯源（LLM 工具不可触达 history/，仅主人可读）。IO 失败直接上抛
    崩溃阻断（user_rules）——留档失败不许覆盖写入。
    """
    from datetime import datetime

    PERSONA_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    source = _persona_path(persona_name)
    if not source.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = PERSONA_HISTORY_DIR / f"{persona_name}_{stamp}.json"
    dest.write_bytes(source.read_bytes())


async def set_persona_kernel_prompt(persona_name: str, kernel_prompt: str) -> PersonaDef:
    """覆盖式更新人格 kernel_prompt 并落盘，返回更新后的人格定义。

    仅基本人格（common）的说话风格区可经此修改（工具层已硬拦，此处兜底）；
    覆盖前自动留档旧版到 history/ 子目录。人格不存在时 get_persona 抛
    KeyError 崩溃阻断（user_rules）。
    """
    if persona_name != COMMON_PERSONA_NAME:
        raise ValueError(
            f"人格 {persona_name!r} 的内核已冻结，仅基本人格 "
            f"（{COMMON_PERSONA_NAME}，基本说话风格区）可经此修改。"
        )
    from dataclasses import replace
    persona = replace(get_persona(persona_name), kernel_prompt=kernel_prompt)
    await asyncio.to_thread(_snapshot_persona, persona_name)
    await persist_persona(persona)
    register_persona(persona)
    logger.info(f"人格 {persona_name} 说话风格区已覆盖并落盘（旧版已留档）")
    return persona


async def update_persona_from_human(persona_name: str, changes: dict) -> PersonaDef:
    """仅由人工编辑 HTTP 入口调用；不解除 LLM 对普通人格内核的冻结。"""
    from dataclasses import replace
    if not isinstance(changes, dict) or not changes or set(changes) - {"scene", "kernel_prompt"}:
        raise ValueError("人工编辑只接受适配场景和人格正文")
    if any(not isinstance(v, str) or not v.strip() for v in changes.values()):
        raise ValueError("人格字段必须为非空文字")
    updated = replace(get_persona(persona_name), **{k: v.strip() for k, v in changes.items()})
    if persona_name == COMMON_PERSONA_NAME and len(updated.kernel_prompt) > COMMON_STYLE_BUDGET:
        raise ValueError(f"基本说话风格最多{COMMON_STYLE_BUDGET}字")
    await asyncio.to_thread(_snapshot_persona, persona_name)
    await persist_persona(updated)
    register_persona(updated)
    return updated
