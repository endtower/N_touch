# -*- coding: utf-8 -*-
"""记忆浓缩 Agent - 负责从当前会话事件中凝练记忆。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传已拼装好的 user 消息
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架
  3. 是否继续 `_should_continue` —— 有工具调用继续，否则终止
  4. 工具处理 `_process_tool` —— 复用父类实现
  5. 后处理 `_postprocess`   —— 返回 LLM 最终文本
"""

from typing import List, Dict, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from config.role_settings import RoleConfig
from brain.hc.database.character_compose import CharacterCompose
from brain.hc.database.group_compose import GroupCompose

logger = get_logger(__name__, "DEBUG")


class MemoryAgent(LightweightAgent):
    """记忆浓缩Agent - 专门负责从会话事件中凝练记忆"""

    def _get_system_prompt_content(self) -> str:
        master_name = RoleConfig.MASTER_NAME
        assistant_name = RoleConfig.ASSISTANT_NAME
        assistant_nick = RoleConfig.ASSISTANT_NICK_NAME

        return """你是记忆浓缩agent，职责是修改记忆数据，辅助下次执行任务或参与会话社交。

【硬性约束】
- 系统已根据 session_id 自动拉取目标节点 uuid，你无法调用向量搜索。
- update_character / update_group 必须传系统给的 uuid。无 uuid 则拒绝执行，不要自行创建节点。
- 仅 modification_type='mentioned_person' 且被提及人物尚未正式存在时，调用 create_fuzzy_role。FuzzyRole 不查重，由其他流程异步处理。
- 调用工具时，有值则覆盖，无值则跳过对应字段。严禁编造不存在的事件。

【会话类型判断】
- 群聊：session_id 前缀为 group_ → 使用 update_group。
- 私聊：session_id 前缀为 private_ → 使用 update_character。

【工具与字段】
- update_character（私聊角色）
  · 必填：character_id（uuid）。
  · 可选：name_list（≤3，按顺序整组替换，首项为最常用名）、impression、familiarity、self_relationship、contacts。
- update_group（群聊）
  · 必填：group_id（uuid）。
  · 可选：name_list（≤3，按顺序整组替换，首项为最常用名）、impression、familiarity、member_impression、self_image、contacts。
  · member_impression 格式："成员名，印象描述；成员名，印象描述"。例："小明，热心且话多，喜欢打游戏；张三，程序员，在深圳创业"。
- create_fuzzy_role（被提及但尚未正式存在的重要人物）
  · 必填：name（被提及人物的名字）、source_session_id（来源会话，会话锚定）、content（自然语言描述）。
  · 不查重，异步流程处理。

【字段填写原则】
- name_list：整组替换（清空原全部别名后重建），最多 3 个，第一个为最常用。
- impression（印象）：对一个人的认知总和，保守且简短，不超过 50 字；只做小幅调整，避免颠覆性改变。
  · 群聊中出现的 A 与私聊中出现的 A 视为不同个体，仅保留少量互通印象。
- familiarity（熟悉程度，私聊 + 群聊通用）：
  · 是对该角色 / 该群聊的熟悉程度，同时也是后续交往 / 在该群中应采取的「策略倾向」的隐含表达。
  · 不超过 20 字，渐进调整，避免每次会话都大幅改动。
- self_relationship（仅私聊）：仅对认定有长期关系、功利价值的人写。在上次基础上渐进调整，不超过 20 字。临时或不重要的人不写。
- self_image（仅群聊）：我在该群聊的姿态与自我认知，用于辅助未来在该群的行为倾向。
  · 保守修改，不超过 20 字。
  · 例："每天发可爱表情暖群，大家都喜欢"；"不参与争吵，只偶尔吐槽自己的经历"。

【返回】
完成修改后，输出一段不超过 50 字的简洁描述。"""

    @property
    def SYSTEM_PROMPT(self) -> str:
        return self._get_system_prompt_content()

    MAX_ALIAS_COUNT = 3

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return {
            "update_character": self._update_character,
            "update_group": self._update_group,
            "create_fuzzy_role": self._create_fuzzy_role,
        }

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "update_character",
                    "description": "更新一个角色的印象。必须传入角色 uuid。无 uuid 则拒绝执行（普通角色需先由 create_fuzzy_role 转正后再用此工具更新）。有值则覆盖，无值则跳过对应字段。name_list 提供时，会清空角色原有的全部别名，并按新列表重建（最多 3 个），即整组替换而非追加。familiarity 表示对该角色的熟悉程度，并隐含后续与他交往时倾向采用的策略（例如「常联络的朋友」→ 自然、放松；「陌生同事」→ 礼貌、克制；「曾起冲突的人」→ 警惕、保持距离）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "character_id": {
                                "type": "string",
                                "description": "角色 uuid，必填，用于定位数据库中的 Character 节点"
                            },
                            "name_list": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 3,
                                "description": "角色名称列表（最多 3 个）。若提供，会清空角色原有的全部别名，并按此列表重建（整组替换）。例如：原别名 [A, B]，传入 [B, C] → 最终别名变为 [B, C]。"
                            },
                            "impression": {
                                "type": "string",
                                "description": "对该角色的印象"
                            },
                            "familiarity": {
                                "type": "string",
                                "description": "熟悉程度，不超过 20 字。"
                            },
                            "self_relationship": {
                                "type": "string",
                                "description": "与自己的关系"
                            },
                            "contacts": {
                                "type": "array",
                                "description": "联系方式列表（session_id 数组）",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "session_id": {
                                            "type": "string",
                                            "description": "会话标识，格式为 \"type_id\"，如 \"private_1234567890\""
                                        }
                                    },
                                    "required": ["session_id"]
                                }
                            }
                        },
                        "required": ["character_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "update_group",
                    "description": "更新一个群聊。必须传入群聊 uuid。无 uuid 则拒绝执行。有值则覆盖，无值则跳过对应字段。name_list 提供时，会清空群聊原有的全部别名，并按新列表重建（最多 3 个），即整组替换而非追加。familiarity 表示对该群聊的熟悉程度，并隐含在该群聊中应采取的策略倾向（例如「常驻核心群」→ 自然参与、主动发言；「偶尔潜水」→ 围观、少主动；「工作汇报群」→ 正式、简洁；「气氛压抑」→ 谨慎、低调）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group_id": {
                                "type": "string",
                                "description": "群聊 uuid，必填，用于定位数据库中的 Group 节点"
                            },
                            "name_list": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 3,
                                "description": "群聊名称列表（最多 3 个）。若提供，会清空群聊原有的全部别名，并按此列表重建（整组替换）。例如：原别名 [A, B]，传入 [B, C] → 最终别名变为 [B, C]。"
                            },
                            "impression": {
                                "type": "string",
                                "description": "对该群聊的印象"
                            },
                            "familiarity": {
                                "type": "string",
                                "description": "熟悉程度，不超过 20 字。"
                            },
                            "member_impression": {
                                "type": "string",
                                "description": "群组成员印象，格式：\"成员名，印象描述；成员名，印象描述\"，多个成员用分号分隔"
                            },
                            "self_image": {
                                "type": "string",
                                "description": "自己在这个群聊的形象"
                            },
                            "contacts": {
                                "type": "array",
                                "description": "联系方式列表（session_id 数组）",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "session_id": {
                                            "type": "string",
                                            "description": "会话标识，格式为 \"type_id\"，如 \"group_198431299\""
                                        }
                                    },
                                    "required": ["session_id"]
                                }
                            }
                        },
                        "required": ["group_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "create_fuzzy_role",
                    "description": "创建一个可能存在/模糊存在的重要角色（FuzzyRole 节点）。锚定到具体来源会话。不查询数据库判断同名角色是否存在，不合并到已有 Character；任务构造器后续会异步处理该节点。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "被提及人物的名字，必填"
                            },
                            "source_session_id": {
                                "type": "string",
                                "description": "由哪个会话提及的（session_id），必填。会话锚定用"
                            },
                            "content": {
                                "type": "string",
                                "description": "关于此人的描述内容（自然语言），必填"
                            }
                        },
                        "required": ["name", "source_session_id", "content"]
                    }
                }
            },
        ]

    def get_system_prompt(self) -> str:
        return self.SYSTEM_PROMPT

    # ===================== 五段式骨架 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：透传已拼装好的 user 消息。"""
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：有工具调用则继续执行并回灌；无工具调用（LLM 收尾）则终止。"""
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：返回 LLM 最终文本。"""
        return {"content": content, "extra_info": extra_info}

    async def _call_llm(self, messages: list, *, mode: str = "basic-reason",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            mode=mode,
            messages=full_messages,
            tools=self.get_available_tools(),
        )

    # ===================== 工具实现 =====================
    async def _replace_character_aliases(self, character_uuid: str, new_aliases: List[str]) -> Dict[str, Any]:
        """按 uuid 整组替换角色别名。"""
        if not character_uuid:
            return {"status": "error", "message": "character_uuid 不能为空"}
        if not new_aliases:
            return {"status": "error", "message": "new_aliases 不能为空"}

        seen = set()
        unique_aliases = []
        for n in new_aliases:
            if n and n not in seen:
                seen.add(n)
                unique_aliases.append(n)

        if len(unique_aliases) > self.MAX_ALIAS_COUNT:
            unique_aliases = unique_aliases[:self.MAX_ALIAS_COUNT]

        try:
            from brain.hc.database.character import CharacterOperations
            char_ops = CharacterOperations()
            await char_ops.replace_aliases(character_uuid, unique_aliases)
            return {"status": "success", "message": f"角色别名已整组替换为: {unique_aliases}"}
        except Exception as e:
            logger.error(f"_replace_character_aliases 失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def _replace_group_aliases(self, group_uuid: str, new_aliases: List[str]) -> Dict[str, Any]:
        """按 uuid 整组替换群聊别名。"""
        if not group_uuid:
            return {"status": "error", "message": "group_uuid 不能为空"}
        if not new_aliases:
            return {"status": "error", "message": "new_aliases 不能为空"}

        seen = set()
        unique_aliases = []
        for n in new_aliases:
            if n and n not in seen:
                seen.add(n)
                unique_aliases.append(n)

        if len(unique_aliases) > self.MAX_ALIAS_COUNT:
            unique_aliases = unique_aliases[:self.MAX_ALIAS_COUNT]

        try:
            from brain.hc.database.group import GroupOperations
            group_ops = GroupOperations()
            await group_ops.replace_aliases(group_uuid, unique_aliases)
            return {"status": "success", "message": f"群聊别名已整组替换为: {unique_aliases}"}
        except Exception as e:
            logger.error(f"_replace_group_aliases 失败: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    @async_handle_errors()
    async def run(
        self,
        session_id: str,
        modification_type: str,
        content: str,
        extra_name: str = None,
    ) -> Dict[str, Any]:
        """MemoryAgent 入口。"""
        if not session_id:
            return {"status": "error", "message": "缺少必填参数 session_id"}
        if modification_type not in ("session", "mentioned_person"):
            return {"status": "error", "message": f"modification_type 必须是 'session' 或 'mentioned_person'，收到: {modification_type}"}
        if not content:
            return {"status": "error", "message": "缺少必填参数 content"}
        if modification_type == "mentioned_person" and not extra_name:
            return {"status": "error", "message": "modification_type='mentioned_person' 时必须提供 extra_name"}

        impression_str, target_uuid = await self._first_impression_search(session_id)

        if modification_type == "session":
            task_intro = (
                f"当前任务：修改本会话，对应 uuid：{target_uuid}。"
                f"系统已根据 session_id 自动拉取目标节点信息（见上文），"
                f"请根据 content 自然语言决定具体修改内容，并调用 update_character 或 update_group 传入上方的 uuid。"
            )
        else:
            task_intro = (
                f"当前任务：在会话（session_id={session_id}）中创建一个被提及的重要人物（{extra_name}）。"
                f"系统已根据 session_id 自动拉取锚定会话信息（见上文）。"
                f"无需调用 update_character/update_group，请调用 create_fuzzy_role 创建 FuzzyRole 节点。"
            )

        message_parts = [
            impression_str,
            task_intro,
            f"\n【待处理内容】\n{content}",
        ]
        if modification_type == "mentioned_person":
            message_parts.append(f"\n【被提及人物名称】\n{extra_name}")

        user_message = "\n".join(message_parts)

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        processed_input = self._preprocess(user_message)
        loop_result = await self._llm_loop(processed_input, mode='basic-reason')
        final_content = loop_result.get("content", "")

        # 工具调用全部结束后，重新拉取一次实体最新信息写入 temporary_memory。
        if modification_type == "session":
            await self._refresh_temporary_memory_after_update(session_id)

        return final_content or "记忆调整完成"

    async def _refresh_temporary_memory_after_update(self, session_id: str) -> None:
        """Agent 修改完毕后，重新拉取实体最新信息写入 temporary_memory（identifier = uuid）。"""
        if not session_id:
            return
        try:
            compose = CharacterCompose()
            result = await compose.first_session_impression_search(name=None, session_id=session_id)
        except Exception as e:
            logger.warning(f"[MemoryAgent] 修改后再拉取 first_session_impression_search 失败: {e}")
            return

        if not result:
            return

        entity_uuid = result.get("uuid")
        formatted_info = result.get("formatted_info") or ""
        if not entity_uuid or not formatted_info:
            return

        try:
            from brain.hc.temporary_memory import get_temporary_memory
            get_temporary_memory().add(
                formatted_info,
                identifier=entity_uuid,
            )
            logger.info(
                f"[MemoryAgent] 已将修改后的实体最新信息写入临时记忆: "
                f"identifier={entity_uuid}"
            )
        except Exception as e:
            logger.warning(f"[MemoryAgent] 刷新临时记忆失败: {e}")

    async def _first_impression_search(self, session_id: str) -> tuple:
        """根据 session_id 自动拉取目标节点信息。返回 (impression_str, target_uuid)。"""
        if not session_id:
            return "（未提供 session_id）", None

        try:
            compose = CharacterCompose()
            result = await compose.first_session_impression_search(name=None, session_id=session_id)
        except Exception as e:
            logger.warning(f"first_session_impression_search 调用失败: {e}")
            return f"（first_session_impression_search 调用失败: {e}）", None

        uuid_val = (result or {}).get("uuid") or None
        formatted_info = (result or {}).get("formatted_info") or ""

        extra_lines = []
        if uuid_val:
            extra_lines.append(f"uuid：{uuid_val}")
        if result.get("created"):
            extra_lines.append("备注：该节点为本次首次创建")

        impression_str = formatted_info
        if extra_lines:
            impression_str = f"{formatted_info}\n" + "\n".join(extra_lines)

        return impression_str, uuid_val

    async def _update_character(self,
                              character_id: str,
                              name_list: Optional[List[str]] = None,
                              impression: Optional[str] = None,
                              familiarity: Optional[str] = None,
                              self_relationship: Optional[str] = None,
                              contacts: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """更新一个普通角色。要求 character_id 必填，无 uuid 拒绝执行。"""
        if not character_id:
            return {"execute": False, "message": "character_id 必填，普通角色必须先经 create_fuzzy_role 转正后获得 uuid 才能更新"}

        try:
            compose = CharacterCompose()
            char_ops = compose.character_ops

            char_info = await char_ops.get_character_full_info(character_id)
            if not char_info:
                return {"execute": False, "message": f"角色 uuid={character_id} 不存在"}

            aliases = char_info.get("aliases", [])
            alias_names = [a.get("name", "") for a in aliases if a.get("name")]
            primary_name = alias_names[0] if alias_names else char_info.get("name", "") or character_id

            if name_list is not None:
                if not isinstance(name_list, list) or len(name_list) == 0:
                    return {"execute": False, "message": "name_list 必须是非空列表"}
                if len(name_list) > self.MAX_ALIAS_COUNT:
                    return {"execute": False, "message": f"name_list 最多 {self.MAX_ALIAS_COUNT} 个"}
                cleaned_name_list = [str(n).strip() for n in name_list if str(n).strip()]
                if not cleaned_name_list:
                    return {"execute": False, "message": "name_list 全为空字符串"}

            updated_fields = []

            if name_list is not None:
                await self._replace_character_aliases(character_id, cleaned_name_list)
                updated_fields.append(f"名称列表（整组替换为: {cleaned_name_list}）")

            if impression:
                updated_fields.append("印象")
            if familiarity:
                updated_fields.append("熟悉程度")
            if self_relationship:
                updated_fields.append("与自己的关系")
            if contacts:
                updated_fields.append("联系方式")

            if not updated_fields:
                return {"execute": True, "message": f"角色 {primary_name} 没有需要更新的字段"}

            current_primary = cleaned_name_list[0] if name_list is not None else primary_name

            overwrite_kwargs = {"name": current_primary}
            if impression:
                overwrite_kwargs["impression"] = impression
            if familiarity:
                overwrite_kwargs["familiarity"] = familiarity
            if self_relationship:
                overwrite_kwargs["self_relationship"] = self_relationship
            if contacts:
                overwrite_kwargs["contacts"] = contacts

            await compose.character_operation("overwrite", **overwrite_kwargs)

            return {
                "execute": True,
                "message": f"角色 {current_primary} 更新成功，更新字段: {', '.join(updated_fields)}",
                "character_id": character_id,
            }

        except Exception as e:
            error_msg = f"更新角色时发生错误: {str(e)}"
            logger.error(f"执行出错: {str(e)}", exc_info=True)
            return {"execute": False, "message": error_msg}

    async def _update_group(self,
                          group_id: str,
                          name_list: Optional[List[str]] = None,
                          impression: Optional[str] = None,
                          familiarity: Optional[str] = None,
                          member_impression: Optional[str] = None,
                          self_image: Optional[str] = None,
                          contacts: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """更新一个群聊。要求 group_id 必填，无 uuid 拒绝执行。"""
        if not group_id:
            return {"execute": False, "message": "group_id 必填，无 uuid 拒绝执行"}

        try:
            group_compose = GroupCompose()
            from brain.hc.database.group import GroupOperations
            group_ops = GroupOperations()

            group_info = await group_ops.get_group_with_details(group_id)
            if not group_info:
                return {"execute": False, "message": f"群聊 uuid={group_id} 不存在"}

            aliases = group_info.get("aliases", [])
            alias_names = [a.get("name", "") for a in aliases if a.get("name")]
            primary_name = alias_names[0] if alias_names else group_info.get("name", "") or group_id

            if name_list is not None:
                if not isinstance(name_list, list) or len(name_list) == 0:
                    return {"execute": False, "message": "name_list 必须是非空列表"}
                if len(name_list) > self.MAX_ALIAS_COUNT:
                    return {"execute": False, "message": f"name_list 最多 {self.MAX_ALIAS_COUNT} 个"}
                cleaned_name_list = [str(n).strip() for n in name_list if str(n).strip()]
                if not cleaned_name_list:
                    return {"execute": False, "message": "name_list 全为空字符串"}

            updated_fields = []

            if name_list is not None:
                await self._replace_group_aliases(group_id, cleaned_name_list)
                updated_fields.append(f"名称列表（整组替换为: {cleaned_name_list}）")

            if impression:
                updated_fields.append("印象")
            if familiarity:
                updated_fields.append("熟悉程度")
            if member_impression:
                updated_fields.append("群组成员印象")
            if self_image:
                updated_fields.append("我在该群聊的形象")
            if contacts:
                updated_fields.append("联系方式")

            if not updated_fields:
                return {"execute": True, "message": f"群聊 {primary_name} 没有需要更新的字段"}

            current_primary = cleaned_name_list[0] if name_list is not None else primary_name

            overwrite_kwargs = {"name": current_primary}
            if impression:
                overwrite_kwargs["impression"] = impression
            if familiarity:
                overwrite_kwargs["familiarity"] = familiarity
            if member_impression:
                overwrite_kwargs["member_impression"] = member_impression
            if self_image:
                overwrite_kwargs["self_image"] = self_image
            if contacts:
                overwrite_kwargs["contacts"] = contacts

            await group_compose.group_operation("overwrite", **overwrite_kwargs)

            return {
                "execute": True,
                "message": f"群聊 {current_primary} 更新成功，更新字段: {', '.join(updated_fields)}",
                "group_id": group_id,
            }

        except Exception as e:
            error_msg = f"更新群聊时发生错误: {str(e)}"
            logger.error(f"执行出错: {str(e)}", exc_info=True)
            return {"execute": False, "message": error_msg}

    async def _create_fuzzy_role(self,
                               name: str,
                               source_session_id: str,
                               content: str) -> Dict[str, Any]:
        """创建一个可能存在/模糊存在的重要角色（FuzzyRole 节点）。"""
        if not name:
            return {"execute": False, "message": "name 必填"}
        if not source_session_id:
            return {"execute": False, "message": "source_session_id 必填（锚定会话）"}
        if not content:
            return {"execute": False, "message": "content 必填"}

        try:
            from brain.hc.database.fuzzy_role import get_fuzzy_role_ops

            fuzzy_ops = get_fuzzy_role_ops()
            result = await fuzzy_ops.create_fuzzy_role(
                name=name,
                source_session_id=source_session_id,
                content=content,
            )

            if result.get("status") != "success":
                return {"execute": False, "message": result.get("message", "创建 FuzzyRole 失败")}

            return {
                "execute": True,
                "message": result.get("message", ""),
                "fuzzy_role_id": result.get("fuzzy_role_id"),
                "name": name,
                "source_session_id": source_session_id,
                "source_entity_type": result.get("source_entity_type"),
                "source_entity_uuid": result.get("source_entity_uuid"),
            }

        except Exception as e:
            error_msg = f"创建 FuzzyRole 时发生错误: {str(e)}"
            logger.error(f"执行出错: {str(e)}", exc_info=True)
            return {"execute": False, "message": error_msg}
