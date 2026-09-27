# -*- coding: utf-8 -*-
"""通用工具：ToolBequest（经验工具：query_bequest / query_decision_bequest /
upsert_bequest / delete_bequest）

原 BequestAgent 侧实现（bequest_agent._upsert_bequest / _delete_bequest 及其
schema）于 2026-09-15 收口迁移至本模块；query_bequest 于 2026-09-17 新增，
schema 与实现统一在此管理，供各 agent 及反思系统（reflection_tools）直接
拼装复用，避免复制漂移。

工具语义（经验库分两类：category 'speech' 话术 / 'decision' 决策，工具按类别定向）：
- query_bequest：按语义相似度检索话术经验（固定 speech，向量比对 title，详见数据库层）
- query_decision_bequest（2026-09-24 新增）：检索决策经验（固定 decision）；
  决策经验是一种过往的面对某种情景的广泛思路；未取得任何经验（检索结果为空）时，
  在返回结果中追加提示——向主人请教、学习经验。不设相似度阈值：相似度随结果
  一并返回，匹配是否够用由 LLM 自行判断
- upsert_bequest：按 title 定位（同名同类别覆盖，否则新建）；category 参数
  （中文枚举「话术」/「决策」）决定登记到哪个经验库；登记前在同类别内自动查重
  （向量比对 title）：近乎相同的经验直接拒绝创建（duplicate_conflict，防 LLM
  忽略软警告堆积重复），疑似重复经验（duplicate_warning）提醒 LLM 调用
  delete_bequest 删除
- delete_bequest：按 title + category 删除对应类别的经验

全异步：底层走 BequestOperations（纯异步），本模块不引入同步 IO。
"""

from typing import Dict, Any, List, Callable

from utils.logger import get_logger

logger = get_logger('ToolBequest', "DEBUG")


class ToolBequest:
    """经验通用工具：query_bequest（检索话术）+ query_decision_bequest（检索决策）
    + upsert_bequest（创建/覆盖）+ delete_bequest（删除）。

    经验库分两类（category）：'speech' 话术经验 / 'decision' 决策经验；
    检索工具各自固定类别，增删工具经 category 参数（中文枚举）定向。
    """

    # upsert 前置查重阈值（2026-09-19 依生产库存量全量相似度矩阵标定：
    # 正常经验对相似度最高 0.58，语义重复对最低 0.83 / 最高 0.98，
    # 空隙区间 (0.58, 0.83)，阈值取空隙内）：
    # - 相似度 >= HARD_DUPLICATE_THRESHOLD：视为近乎相同经验。
    #   新建场景直接拒绝创建（返回 duplicate_conflict），由 LLM 删重后重试
    #   或沿用已有经验 / 换可区分的 title——软警告已被实测证明会被 LLM 忽略
    #   （0.98 相似度的重复对曾正常发出警告仍被忽略并堆积入库），必须硬拦。
    # - 相似度 >= DUPLICATE_SIMILARITY_THRESHOLD 且未被硬拦：随结果返回
    #   duplicate_warning，提醒 LLM 对比语义、自行调用 delete_bequest 删重。
    HARD_DUPLICATE_THRESHOLD = 0.80
    DUPLICATE_SIMILARITY_THRESHOLD = 0.70

    # 类别枚举：schema 面向 LLM 用中文（同 ToolVision 惯例），数据库 category
    # 用英文，转换统一收口在 _resolve_category。
    _CATEGORY_TO_DB = {"话术": "speech", "决策": "decision"}

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_query_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_bequest 工具的 schema（2026-09-17 新增）。"""
        return {
            "type": "function",
            "function": {
                "name": "query_bequest",
                "description": (
                    "检索已有话术经验。输入当前处境或想表达的意思，"
                    "返回语义最相似的话术经验（标题 + 内容 + 相似度）。"
                    "用于：说话前找现成技巧、登记新经验前查重。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索内容：描述当前处境、想表达的意思或要查重的经验主题",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回条数上限，默认 3",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    @classmethod
    def get_query_decision_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 query_decision_bequest 工具的 schema（2026-09-24 新增，参数同 query_bequest）。"""
        return {
            "type": "function",
            "function": {
                "name": "query_decision_bequest",
                "description": (
                    "检索已有决策经验。输入当前面临的情景或要做的判断，"
                    "返回语义最相似的决策经验（标题 + 内容 + 相似度）。"
                    "决策经验是一种过往的面对某种情景的广泛思路。"
                    "用于：决策前参考既往思路、登记新决策经验前查重。"
                    "返回结果含相似度，匹配是否够用自行判断；"
                    "若未取得任何经验，说明库中暂无可用思路，"
                    "此时应向主人请教、学习经验，不要凭空硬编。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索内容：描述当前面临的情景、要做的判断或要查重的经验主题",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回条数上限，默认 3",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    @classmethod
    def get_upsert_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 upsert_bequest 工具的 schema（原样迁自 bequest_agent，2026-09-15；
        2026-09-24 增加 category 参数以支持登记决策经验）。"""
        return {
            "type": "function",
            "function": {
                "name": "upsert_bequest",
                "description": (
                    "创建经验（话术经验或决策经验）。如果同类中已存在同名经验则覆盖内容，否则创建新的经验。"
                    "category 决定登记到哪个经验库："
                    "「话术」是发言/回复技巧类，"
                    "评判标准是是否能更加快速便捷的表达出具体意思，而不是对于事件结果的影响"
                    "示例： 标题：表达惊讶，滑稽，震撼。 内容：可以用‘卧槽啊！’表示对过于离谱的事情的震惊。"
                    "错误示例：遇到对方明显不想搭理、摆烂或怼人的回复，直接发「……」（或无语流汗表情）表示无语，不解释、不追问、不给台阶。（因为夹杂了不解释、不追问、不给台阶这些无意义的内容）"
                    "「决策」是决策经验，一种过往的面对某种情景的广泛思路"
                    "（面对这类情景大致怎么想、怎么处理，不绑定具体话术与具体事件）。"
                    "登记前系统会自动查重（同类内）：若已存在近乎相同的经验，本次创建将被拒绝（返回 duplicate_conflict），"
                    "请按返回的 hint 处理（沿用已有经验 / 删除旧的再重试 / 换用语义可区分的标题）。"
                    "登记完成后会自动返回系统内 3 条语义近似的经验及相似度；若返回 duplicate_warning，"
                    "说明存在疑似重复经验，必须逐条对比语义：属重复的必须调用 delete_bequest 删除"
                    "（保留语义最准确的一条），确有差异的保留不动。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "标题"},
                        "summary": {"type": "string", "description": "内容"},
                        "category": {
                            "type": "string",
                            "enum": ["话术", "决策"],
                            "description": (
                                "经验类别：「话术」= 发言/回复技巧；"
                                "「决策」= 面对某种情景的广泛思路"
                            ),
                        },
                    },
                    "required": ["title", "summary", "category"]
                }
            }
        }

    @classmethod
    def get_delete_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 delete_bequest 工具的 schema（原样迁自 bequest_agent，2026-09-15；
        2026-09-24 增加 category 参数以支持删除决策经验）。"""
        return {
            "type": "function",
            "function": {
                "name": "delete_bequest",
                "description": "删除经验。如果已登记的经验被认为不恰当，删除该经验（按 category 指明删哪一类）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "标题"},
                        "category": {
                            "type": "string",
                            "enum": ["话术", "决策"],
                            "description": (
                                "经验类别：「话术」= 发言/回复技巧；"
                                "「决策」= 面对某种情景的广泛思路"
                            ),
                        },
                    },
                    "required": ["title", "category"]
                }
            }
        }

    @classmethod
    def get_bequest_tool_schemas(cls) -> List[Dict[str, Any]]:
        """返回经验工具 schema 列表（query_bequest / upsert_bequest / delete_bequest，
        供工具表直接拼装；query_decision_bequest 单独取 `get_query_decision_bequest_tool_schema()`）。"""
        return [
            cls.get_query_bequest_tool_schema(),
            cls.get_upsert_bequest_tool_schema(),
            cls.get_delete_bequest_tool_schema(),
        ]

    # ============ 工具实现 ============

    @classmethod
    def _resolve_category(cls, category: str) -> str:
        """中文类别枚举（LLM 传入）转数据库 category；非法值抛 ValueError 走 LLM 自纠。"""
        db_category = cls._CATEGORY_TO_DB.get((category or "").strip())
        if not db_category:
            raise ValueError(
                f"category 非法（{category!r}）：只能取「话术」或「决策」。"
            )
        return db_category

    @classmethod
    async def _query_by_category(
        cls, query: str, top_k: int, category: str
    ) -> Dict[str, Any]:
        """按类别做向量检索并补齐标题/内容（query_bequest 与决策查询共用内核）。

        委托 BequestOperations.search_bequest_by_vector，再补齐每条经验
        的标题与内容。向量嵌入功能禁用或查询向量生成失败时，数据库层
        返回空结果（返回 count=0，不阻断调用方）。
        """
        from brain.hc.database.bequest import BequestOperations

        bequest_ops = BequestOperations()
        hits = await bequest_ops.search_bequest_by_vector(
            query, top_k=top_k, category=category,
        )

        results = []
        for bequest_id, sim in hits:
            bequest = await bequest_ops.get_bequest_by_id(bequest_id)
            if bequest:
                results.append({
                    "title": bequest.get("title", ""),
                    "summary": bequest.get("summary", ""),
                    "similarity": round(sim, 4),
                })
        return {"query": query, "count": len(results), "bequests": results}

    @classmethod
    async def query_bequest(cls, query: str, top_k: int = 3) -> Dict[str, Any]:
        """按语义相似度检索话术经验（category 固定 'speech'，向量比对 title）。

        Returns:
            {"query": str, "count": int,
             "bequests": [{"title": str, "summary": str, "similarity": float}]}
        """
        result = await cls._query_by_category(query, top_k, "speech")
        logger.debug(f"query_bequest 完成: query={query}, hits={result['count']}")
        return result

    @classmethod
    async def query_decision_bequest(cls, query: str, top_k: int = 3) -> Dict[str, Any]:
        """按语义相似度检索决策经验（category 固定 'decision'，向量比对 title）。

        决策经验是一种过往的面对某种情景的广泛思路。
        检索结果为空（未取得任何经验）时，在结果中追加 hint：向主人请教、
        学习经验。不设相似度阈值——相似度随结果一并返回，匹配是否够用
        由调用方 LLM 自行判断。

        Returns:
            {"query": str, "count": int, "hint": str,  # hint 仅在检索结果为空时出现
             "bequests": [{"title": str, "summary": str, "similarity": float}]}
        """
        result = await cls._query_by_category(query, top_k, "decision")
        logger.debug(f"query_decision_bequest 完成: query={query}, hits={result['count']}")
        if result["count"] == 0:
            result["hint"] = (
                "未取得任何决策经验（库中无匹配）。"
                "此处不要凭空硬编思路，可向主人请教、学习其经验。"
            )
        return result

    @classmethod
    async def upsert_bequest(
        cls, title: str, summary: str, category: str = "话术"
    ) -> Dict[str, Any]:
        """创建或覆盖经验（category 中文枚举「话术」/「决策」，缺省「话术」），落库前先查重。

        按 title + category 精确匹配定位：命中则覆盖 summary，否则新建。
        落库前在同类内按 title 向量检索近似经验（覆盖场景排除自身，一次检索同时
        服务硬拦截 / 软警告 / 近似经验展示）：
        - 新建场景存在相似度 >= HARD_DUPLICATE_THRESHOLD 的近似经验：
          直接拒绝创建，返回 duplicate_conflict，由 LLM 删重后重试、
          沿用已有经验或换可区分的 title（软警告实测会被忽略，必须硬拦）；
        - 相似度 >= DUPLICATE_SIMILARITY_THRESHOLD 且未被硬拦：随结果返回
          duplicate_warning，提醒调用方 LLM 对比语义并调用 delete_bequest 删重。

        Returns:
            正常落库: {"bequest_id": str, "title": str, "category": str,
                       "is_new": bool,
                       "similar_bequests": [{"title": str, "summary": str,
                                             "similarity": float}],
                       "duplicate_warning": str  # 仅存在疑似重复时出现}
            硬重复拒绝: {"error": "duplicate_conflict", "title": str,
                         "conflict_with": [...], "hint": str}
        """
        from brain.hc.database.bequest import BequestOperations

        db_category = cls._resolve_category(category)
        bequest_ops = BequestOperations()
        existing_id = await bequest_ops.get_bequest_id_by_title(title, category=db_category)

        # —— 前置查重（落库前，此时新建场景自身尚未入库无需排除）——
        hits = await bequest_ops.search_bequest_by_vector(
            title, top_k=4, category=db_category,
        )
        similar = []
        for hit_id, sim in hits:
            if existing_id and hit_id == existing_id:
                continue  # 覆盖场景排除自身
            bequest = await bequest_ops.get_bequest_by_id(hit_id)
            if bequest:
                similar.append({
                    "title": bequest.get("title", ""),
                    "summary": bequest.get("summary", ""),
                    "similarity": round(sim, 4),
                })
        similar = similar[:3]

        hard_conflicts = [
            s for s in similar
            if s["similarity"] >= cls.HARD_DUPLICATE_THRESHOLD
        ]

        # 新建场景存在近乎相同的经验：拒绝创建，交 LLM 自纠
        if hard_conflicts and not existing_id:
            conflict_titles = "、".join(
                f"「{d['title']}」(相似度{d['similarity']})" for d in hard_conflicts
            )
            logger.debug(
                f"upsert_bequest 拒绝（硬重复）: title={title}, "
                f"category={db_category}, 冲突={conflict_titles}"
            )
            return {
                "error": "duplicate_conflict",
                "title": title,
                "conflict_with": hard_conflicts,
                "hint": (
                    f"已存在近乎相同的经验: {conflict_titles}。本次创建被拒绝。"
                    "若属重复经验，请直接沿用已有经验（无需重复登记），"
                    "或调用 delete_bequest 删除旧的后再重新 upsert；"
                    "若确有差异，请换用语义上可区分的 title 重新 upsert。"
                ),
            }

        # —— 落库（覆盖或新建）——
        if existing_id:
            await bequest_ops.update_bequest(title, category=db_category, summary=summary)
            is_new = False
            bequest_id = existing_id
        else:
            bequest_id = await bequest_ops.create_bequest(
                title=title, summary=summary, category=db_category,
            )
            is_new = True

        soft_duplicates = [
            s for s in similar
            if s["similarity"] >= cls.DUPLICATE_SIMILARITY_THRESHOLD
        ]

        logger.debug(
            f"upsert_bequest 完成: title={title}, category={db_category}, "
            f"is_new={is_new}, 近似经验={len(similar)}, 疑似重复={len(soft_duplicates)}"
        )
        result: Dict[str, Any] = {
            "bequest_id": bequest_id,
            "title": title,
            "category": db_category,
            "is_new": is_new,
            "similar_bequests": similar,
        }
        if soft_duplicates:
            dup_titles = "、".join(f"「{d['title']}」(相似度{d['similarity']})" for d in soft_duplicates)
            result["duplicate_warning"] = (
                f"检测到疑似重复经验: {dup_titles}。请逐条对比语义："
                "若属重复经验，必须调用 delete_bequest 删除多余的一条"
                "（保留语义最准确的一条）；若确有差异则保留，无需处理。"
            )
        return result

    @classmethod
    async def delete_bequest(cls, title: str, category: str = "话术") -> Dict[str, Any]:
        """删除经验（按 title + category 精确匹配，category 缺省「话术」）。

        Returns:
            {"title": str, "category": str, "success": bool}
        """
        from brain.hc.database.bequest import BequestOperations

        db_category = cls._resolve_category(category)
        bequest_ops = BequestOperations()
        success = await bequest_ops.delete_bequest_by_title(title, category=db_category)
        logger.debug(
            f"delete_bequest 完成: title={title}, category={db_category}, success={success}"
        )
        return {"title": title, "category": db_category, "success": success}

    # ============ 分发表 ============

    @classmethod
    def get_tool_functions(cls) -> Dict[str, Callable]:
        """返回工具名 -> 实现的分发表（供 agent 的 _initialize_tool_functions 直接使用）。"""
        return {
            "query_bequest": cls.query_bequest,
            "query_decision_bequest": cls.query_decision_bequest,
            "upsert_bequest": cls.upsert_bequest,
            "delete_bequest": cls.delete_bequest,
        }
