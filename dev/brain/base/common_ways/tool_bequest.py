# -*- coding: utf-8 -*-
"""通用工具：ToolBequest（经验工具：query_bequest / query_decision_bequest /
upsert_bequest / delete_bequest）

原 BequestAgent 侧实现（bequest_agent._upsert_bequest / _delete_bequest 及其
schema）于 2026-09-15 收口迁移至本模块；query_bequest 于 2026-09-17 新增，
schema 与实现统一在此管理，供各 agent 及反思系统（reflection_tools）直接
拼装复用，避免复制漂移。

工具语义（经验库分三类：category 'speech' 话术 / 'style' 风格样本 / 'decision' 决策，
工具按类别定向）：
- query_bequest：按语义相似度检索话术或风格样本（向量比对 title，详见数据库层）
- query_decision_bequest（2026-09-24 新增）：检索决策经验（固定 decision）；
  决策经验是一种过往的面对某种情景的广泛思路；未取得任何经验（检索结果为空）时，
  在返回结果中追加提示——向主人请教、学习经验。不设相似度阈值：相似度随结果
  一并返回，匹配是否够用由 LLM 自行判断
- upsert_bequest：按 title 定位（同名同类别覆盖，否则新建）；category 参数
  （中文枚举「话术」/「风格」/「决策」）决定登记到哪个经验库。
  「话术」登记前在同类别内自动查重（向量比对 title）：近乎相同的经验直接拒绝
  创建（duplicate_conflict，防 LLM 忽略软警告堆积重复），疑似重复经验
  （duplicate_warning）提醒 LLM 调用 delete_bequest 删除；
  「风格」是原话样本库，多个样本天然来自同一说话人，不做标题查重，只按同名覆盖
- delete_bequest：按 title + category 删除对应类别的经验

2026-11-04：新增「风格」类别，与「话术」分成两条录入/注入路径：
- 话术＝固定搭配：title 是语用意图，summary 是 2-4 个可直接说出的原话短语；
- 风格＝说话样本：title 是语体与情绪标签，summary 是原话照录（不改写、不总结）。

2026-09-27 起，决策经验相关工具（query_decision_bequest 及 upsert/delete 的
「决策」类别）已从 LLM 视野移除：schema 不再暴露给 LLM，但本模块的通用实现
（方法、category 参数、_resolve_category 等）全部保留，供程序侧按需直接调用。

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
    _CATEGORY_TO_DB = {"话术": "speech", "风格": "style", "决策": "decision"}

    # 录入长度硬约束（字符）：防止反思把「样本」写成场景叙事或使用说明，
    # 最终退化成读不出重点的一坨。超限抛 ValueError 走 LLM 自纠通道。
    _TITLE_MAX_CHARS = 20
    _SUMMARY_MAX_CHARS = {"speech": 30, "style": 60, "decision": 200}

    # query_bequest 检索方式（2026-09-29 新增）：向量 = 语义相似度检索；
    # 标题 = 标题关键词比对（不生成向量，命中少，供反思系统省 token）。
    _QUERY_MODES = ("向量", "标题")

    # ============ 工具 schema（提示词） ============

    @classmethod
    def get_query_bequest_tool_schema(cls, mode_selectable: bool = False) -> Dict[str, Any]:
        """返回 query_bequest 工具的 schema（2026-09-17 新增）。

        mode_selectable=True 时额外暴露「检索方式」参数（向量 / 标题比对），
        供反思系统使用：标题比对不走向量、命中条数少，显著省 token
        （2026-09-29 新增）。默认 False，其他 agent 的 schema 不变。
        """
        properties: Dict[str, Any] = {
            "query": {
                "type": "string",
                "description": "检索内容：描述当前处境、想表达的意思或要查重的经验主题",
            },
            "top_k": {
                "type": "integer",
                "description": "返回条数上限，默认 3",
            },
            "category": {
                "type": "string",
                "enum": ["话术", "风格"],
                "description": (
                    "要检索的经验类别，默认「话术」。"
                    "「话术」查固定搭配；「风格」查他人原话样本。"
                ),
            },
        }
        description = (
            "检索已有经验。输入当前处境或想表达的意思，"
            "返回语义最相似的经验（标题 + 内容 + 相似度）。"
            "「话术」给可直接说出的固定说法；「风格」给他人的原话样本。"
            "用于：说话前找参考、登记新经验前查重。"
        )
        if mode_selectable:
            properties["mode"] = {
                "type": "string",
                "enum": ["向量", "标题"],
                "description": (
                    "检索方式。「向量」：按语义相似度检索（默认），可能带回若干"
                    "语义相近但非目标的条目；「标题」：拿 query 按标题关键词比对，"
                    "命中更少更准、开销更小，适合已知道要找哪条经验时。"
                    "标题方式未命中时不会自动退回向量，需自行改用向量方式。"
                ),
            }
            description += (
                "可选检索方式：向量（语义相似）/ 标题（标题关键词比对，命中更少更省）。"
            )
        return {
            "type": "function",
            "function": {
                "name": "query_bequest",
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
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
        """返回 upsert_bequest 工具的 schema（2026-11-04 改为两条录入路径）。

        类别只暴露「话术」/「风格」；「决策」保留给程序侧直接调用，不对 LLM 开放。
        """
        return {
            "type": "function",
            "function": {
                "name": "upsert_bequest",
                "description": (
                    "登记一条经验，必须从「话术」/「风格」中选一类；同名覆盖，否则新建。\n"
                    "两类都只能录入他人的发言原话，禁止录入思路、策略、感受、效果评价或使用说明。\n"
                    "【话术·固定搭配】title 写 2-4 字语用意图（如「质疑」「惊讶」「收口」），"
                    "不要写场景；summary 只写 1-3 个可直接说出的短俚语/短句，总长 ≤30 字，"
                    "不解释、不评价。示例：title「质疑」summary「真的假的？」；"
                    "title「惊讶」summary「我勒个豆」。\n"
                    "【风格·说话样本】title 写 2-6 字语体与情绪标签（如「冷淡短句·收口」），"
                    "不要写具体事件；summary 原样照录 1 句有辨识度的他人原话，≤60 字，"
                    "不改写、不总结、不附使用说明。\n"
                    "选材标准：只挑劲最大、情绪最强烈、辨识度最高的说法；"
                    "不考虑后果，不考虑道理和理性影响，不因「这样说可能不合适」而放弃。\n"
                    "「话术」登记前会自动查重：近乎相同的经验会被拒绝（duplicate_conflict），"
                    "按返回 hint 处理；返回 duplicate_warning 时必须逐条对比、删除重复项。"
                    "「风格」不做标题查重，同名覆盖即可。\n"
                    "可选 persona：仅当这条经验明确只属于某个人格的应对方式时传，"
                    "通用技巧不要传；不存在的人格名会被拒绝。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["话术", "风格"],
                            "description": (
                                "经验类别。话术＝可直接说出的固定搭配；"
                                "风格＝可模仿的他人原话样本。"
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "话术：2-4 字语用意图（≤20字），不写场景；"
                                "风格：2-6 字语体与情绪标签（≤20字），不写具体事件。"
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "话术：1-3 个可直接说出的短俚语/短句（≤30字），不解释；"
                                "风格：原话照录 1 句（≤60字），不改写、不总结、不加说明。"
                            ),
                        },
                        "persona": {
                            "type": "string",
                            "description": (
                                "可选。给这条经验打人格标签（传人格清单里已有的名字），"
                                "标记它属于该人格的应对方式；"
                                "通用技巧不要传（默认全局通用）。"
                                "传了不存在的人格名会被直接拒绝。"
                            ),
                        },
                    },
                    "required": ["category", "title", "summary"]
                }
            }
        }

    @classmethod
    def get_delete_bequest_tool_schema(cls) -> Dict[str, Any]:
        """返回 delete_bequest 工具的 schema（2026-11-04 增加「风格」类别）。"""
        return {
            "type": "function",
            "function": {
                "name": "delete_bequest",
                "description": "删除一条经验。必须指定类别与标题；不恰当、重复或过时的经验才删除。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["话术", "风格"],
                            "description": "要删除的经验类别。",
                        },
                        "title": {"type": "string", "description": "标题"},
                    },
                    "required": ["category", "title"]
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
                f"category 非法（{category!r}）：只能取「话术」/「风格」/「决策」。"
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
            query, top_k=top_k, category=category, record_usage=True,
        )

        results = []
        for bequest_id, sim in hits:
            bequest = await bequest_ops.get_bequest_by_id(bequest_id)
            if bequest:
                results.append({
                    "bequest_id": bequest_id,
                    "title": bequest.get("title", ""),
                    "summary": bequest.get("summary", ""),
                    "similarity": round(sim, 4),
                })
        return {"query": query, "count": len(results), "bequests": results}

    @classmethod
    async def _query_by_title(
        cls, query: str, top_k: int, db_category: str = "speech"
    ) -> Dict[str, Any]:
        """按标题关键词比对检索经验（2026-09-29 新增，供反思系统省 token 用）。

        不生成查询向量、命中条数通常远少于向量检索；未命中时直接返回空结果
        并附 hint，不静默退回向量（否则失去省 token 的意义）。
        """
        from brain.hc.database.bequest import BequestOperations

        bequest_ops = BequestOperations()
        hits = await bequest_ops.search_bequests_by_title(
            query, category=db_category, limit=top_k,
        )
        await bequest_ops.record_retrievals([h["bequest_id"] for h in hits])
        results = [
            {"title": h["title"], "summary": h["summary"]}
            for h in hits
        ]
        result: Dict[str, Any] = {
            "query": query,
            "mode": "标题",
            "count": len(results),
            "bequests": results,
        }
        if not results:
            result["hint"] = (
                "标题未命中任何经验。若需要语义上的相近经验，"
                "请改用「向量」方式重新查询。"
            )
        logger.debug(f"query_bequest(标题) 完成: query={query}, hits={result['count']}")
        return result

    @classmethod
    async def query_bequest(
        cls, query: str, top_k: int = 3, mode: str = "向量", category: str = "话术"
    ) -> Dict[str, Any]:
        """检索话术或风格样本（category 中文枚举「话术」/「风格」，缺省「话术」）。

        mode：「向量」（默认）按语义相似度检索（向量比对 title）；
        「标题」按标题关键词比对，命中更少更省 token。

        Returns:
            向量: {"query": str, "count": int,
                   "bequests": [{"title": str, "summary": str, "similarity": float}]}
            标题: {"query": str, "mode": "标题", "count": int,
                   "bequests": [{"title": str, "summary": str}],
                   "hint": str}  # hint 仅在未命中时出现
        """
        mode = (mode or "向量").strip()
        if mode not in cls._QUERY_MODES:
            raise ValueError(
                f"检索方式非法（{mode!r}）：只能取「向量」或「标题」。"
            )
        db_category = cls._resolve_category(category)
        if mode == "标题":
            return await cls._query_by_title(query, top_k, db_category)

        result = await cls._query_by_category(query, top_k, db_category)
        logger.debug(
            f"query_bequest 完成: query={query}, category={db_category}, "
            f"hits={result['count']}"
        )
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
        cls, title: str, summary: str, category: str = "话术", persona: str = ""
    ) -> Dict[str, Any]:
        """创建或覆盖经验（category 中文枚举「话术」/「风格」/「决策」，缺省「话术」）。

        按 title + category 精确匹配定位：命中则覆盖 summary（persona 非空时
        一并更新标签，否则保留原标签），否则新建。persona 非空时必须为已注册
        人格名（硬拦，不存在直接 ValueError 走 LLM 自纠）；''＝全局通用。
        2026-10-01 观察期：persona 标签仅落库，检索侧暂不按其过滤。
        2026-11-04：录入长度硬约束（话术/风格分别限制 summary），防止反思把
        样本写成场景叙事或使用说明；「风格」样本不做标题查重（同源样本天然相似，
        查重会误杀），只按同名覆盖。
        话术/决策落库前在同类内按 title 向量检索近似经验（覆盖场景排除自身，
        一次检索同时服务硬拦截 / 软警告 / 近似经验展示）：
        - 新建场景存在相似度 >= HARD_DUPLICATE_THRESHOLD 的近似经验：
          直接拒绝创建，返回 duplicate_conflict，由 LLM 删重后重试、
          沿用已有经验或换可区分的 title（软警告实测会被忽略，必须硬拦）；
        - 相似度 >= DUPLICATE_SIMILARITY_THRESHOLD 且未被硬拦：随结果返回
          duplicate_warning，提醒调用方 LLM 对比语义并调用 delete_bequest 删重。

        Returns:
            正常落库: {"bequest_id": str, "title": str, "category": str,
                       "persona": str, "is_new": bool,
                       "similar_bequests": [{"title": str, "summary": str,
                                             "similarity": float}],
                       "duplicate_warning": str  # 仅存在疑似重复时出现}
            硬重复拒绝: {"error": "duplicate_conflict", "title": str,
                         "conflict_with": [...], "hint": str}
        """
        from brain.hc.database.bequest import BequestOperations
        from brain.base.persona.persona_registry import list_personas

        persona = (persona or "").strip()
        if persona:
            valid = {p.persona_name for p in list_personas()}
            if persona not in valid:
                raise ValueError(
                    f"persona 非法（{persona!r}）：不存在该人格。可用人格: "
                    f"{'、'.join(sorted(valid))}（不绑定请不传 persona）。"
                )

        db_category = cls._resolve_category(category)
        title = (title or "").strip()
        summary = (summary or "").strip()
        if not title or not summary:
            raise ValueError("title 与 summary 都不能为空。")
        if len(title) > cls._TITLE_MAX_CHARS:
            raise ValueError(
                f"title 超长（{len(title)} 字 > 上限 {cls._TITLE_MAX_CHARS} 字）："
                "只写语用意图或语体标签，不要写场景叙事。"
            )
        summary_max = cls._SUMMARY_MAX_CHARS.get(db_category, 200)
        if len(summary) > summary_max:
            raise ValueError(
                f"summary 超长（{len(summary)} 字 > 上限 {summary_max} 字）："
                "话术只写原话短语（不解释），风格只照录原话（不总结、不加说明）。"
            )

        bequest_ops = BequestOperations()
        existing_id = await bequest_ops.get_bequest_id_by_title(title, category=db_category)

        # —— 前置查重（话术/决策按标题向量查重；风格样本天然同源，跳过）——
        similar = []
        if db_category != "style":
            hits = await bequest_ops.search_bequest_by_vector(
                title, top_k=4, category=db_category,
            )
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
            update_kwargs: Dict[str, Any] = {"summary": summary}
            if persona:
                update_kwargs["persona"] = persona  # 非空才改标签，否则保留原标签
            await bequest_ops.update_bequest(title, category=db_category, **update_kwargs)
            is_new = False
            bequest_id = existing_id
        else:
            bequest_id = await bequest_ops.create_bequest(
                title=title, summary=summary, category=db_category, persona=persona,
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
            "persona": persona,
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
        category 可取「话术」/「风格」/「决策」（后两者仅供程序侧）。

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
