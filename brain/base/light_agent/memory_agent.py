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
from brain.hc.database.character_compose import CharacterCompose
from brain.hc.database.group_compose import GroupCompose

logger = get_logger(__name__, "DEBUG")


class MemoryAgent(LightweightAgent):
    """记忆浓缩Agent - 专门负责从会话事件中凝练记忆"""

    history_prefix: str = "memory"

    def _get_system_prompt_content(self) -> str:
        return """你是记忆浓缩agent，职责是修改记忆数据，辅助下次执行任务或参与会话社交。

【职责】
- 只依据【待处理内容】调整，严禁编造不存在的事件；有值则覆盖，无值则跳过对应字段。
- 群聊（session_id 前缀 group_）调用 update_group；私聊（前缀 private_）调用 update_character。
- 群聊中的 A 与私聊中的 A 视为不同个体，仅保留少量互通印象。
- 调整取渐进式：印象只做小幅修正，熟悉程度不因一次会话大幅改动。

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
                    "description": "更新一个角色的印象。必须传入角色 uuid。有值则覆盖，无值则跳过对应字段。name_list 提供时整组替换原别名（最多 3 个），而非追加。",
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
                                "description": "角色名称列表（最多 3 个），整组替换原有别名；第一个为最常用。"
                            },
                            "impression": {
                                "type": "string",
                                "description": "对该角色的印象，不超过 50 字。"
                            },
                            "familiarity": {
                                "type": "string",
                                "description": "熟悉程度，不超过 20 字；也可写与该对象交流时的策略倾向。"
                            },
                            "self_relationship": {
                                "type": "string",
                                "description": "与自己的关系及其价值，不超过 20 字。"
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
                    "description": "更新一个群聊。必须传入群聊 uuid。有值则覆盖，无值则跳过对应字段。name_list 提供时整组替换原别名（最多 3 个），而非追加。",
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
                                "description": "群聊名称列表（最多 3 个），整组替换原有别名；第一个为最常用。"
                            },
                            "impression": {
                                "type": "string",
                                "description": "对该群聊的印象，不超过 50 字。"
                            },
                            "familiarity": {
                                "type": "string",
                                "description": "熟悉程度，不超过 20 字；也可写在该群交流时的策略倾向。"
                            },
                            "member_impression": {
                                "type": "string",
                                "description": "群组成员印象，格式：\"成员名，印象描述；成员名，印象描述\"，多个成员用分号分隔"
                            },
                            "self_image": {
                                "type": "string",
                                "description": "我在该群聊的姿态与自我认知，不超过 20 字。"
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

    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            level=level,
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

        from brain.hc.database.character import CharacterOperations
        char_ops = CharacterOperations()
        await char_ops.replace_aliases(character_uuid, unique_aliases)
        return {"status": "success", "message": f"角色别名已整组替换为: {unique_aliases}"}

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

        from brain.hc.database.group import GroupOperations
        group_ops = GroupOperations()
        await group_ops.replace_aliases(group_uuid, unique_aliases)
        return {"status": "success", "message": f"群聊别名已整组替换为: {unique_aliases}"}

    async def run(
        self,
        session_id: str,
        modification_type: str,
        content: str,
        extra_name: str = None,
    ) -> Dict[str, Any]:
        """MemoryAgent 入口。失败直接上抛阻断，不回退默认值。"""
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
                f"当前任务：修改本会话（uuid：{target_uuid}）。"
                f"请根据 content 决定修改内容，调用 update_character 或 update_group 并传入该 uuid。"
            )
        else:
            task_intro = (
                f"当前任务：在会话（session_id={session_id}）中创建一个被提及的重要人物（{extra_name}）。"
                f"请调用 create_fuzzy_role 创建 FuzzyRole 节点。"
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
        loop_result = await self._llm_loop(processed_input)
        final_content = loop_result.get("content", "")

        # 工具调用全部结束后，重新拉取一次实体最新信息写入 temporary_memory。
        if modification_type == "session":
            await self._refresh_temporary_memory_after_update(session_id)

        return final_content or "记忆调整完成"

    async def _refresh_temporary_memory_after_update(self, session_id: str) -> None:
        """Agent 修改完毕后，重新拉取实体最新信息写入 temporary_memory（identifier = uuid）。"""
        if not session_id:
            return
        compose = CharacterCompose()
        result = await compose.first_session_impression_search(name=None, session_id=session_id)

        if not result:
            return

        entity_uuid = result.get("uuid")
        formatted_info = result.get("formatted_info") or ""
        if not entity_uuid or not formatted_info:
            return

        # 写入临时记忆失败直接上抛崩溃阻断（硬约定 #4），不吞
        from brain.hc.temporary_memory import get_temporary_memory
        get_temporary_memory().add(
            formatted_info,
            identifier=entity_uuid,
        )
        logger.info(
            f"[MemoryAgent] 已将修改后的实体最新信息写入临时记忆: "
            f"identifier={entity_uuid}"
        )

    async def _first_impression_search(self, session_id: str) -> tuple:
        """根据 session_id 自动拉取目标节点信息。返回 (impression_str, target_uuid)。"""
        if not session_id:
            return "（未提供 session_id）", None

        compose = CharacterCompose()
        result = await compose.first_session_impression_search(name=None, session_id=session_id)

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
