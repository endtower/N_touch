# N_Touch（Abyss Core）

一个类脑架构的 AI 聊天机器人后端：外部消息经适配层写入消息库，注意力系统以
**游标轮询**驱动 PFCA 决策层，再分发给对话代理；情绪系统（amy）与状态/记忆系统（hc）
贯穿全程。QQ 侧经 NapCat 系实现 SnowLuma（OneBot v11）接入，另带 Vue3 网页前端。

> 本仓库由 Abyss_Core0.71 干净副本逐步重构而来（内部代号 N_Touch 0.1）。

## 对外安装与 Core 更新（0.4.0 起）

普通用户只下载 `AbyssLauncher.exe`。首次运行释放随包管理服务与更新器，
在启动器选择并确认安装 Core、SnowLuma；Core 包包含可修改的源码、独立 Python
运行环境和匹配的 Web UI，无须用户安装 Python、Node 或构建前端。
首次启动填写自己的身份与模型接入，SnowLuma 登录由用户完成。

Core **只自动检查新版**，不会自动下载、安装或在空闲时切换。下载与安装分别确认，
安装确认展示破坏性改动、组件兼容要求、数据迁移说明和本地修改。
发现本地程序修改时默认阻止覆盖；安装前保留程序与数据备份。
程序回退不会自动覆盖用户数据库；迁移后启动失败需人工确认数据恢复。

对外构建使用 `scripts/build_public_release.py`，详细步骤见
[更新与发布](updater/README.md)。`scripts/build_release.py` 继续用于旧二进制组件的
本机验收；其开发目录描述文件不能作为公开安装布局。公共默认模块在
`release_defaults/`，个人配置与记忆不进入源码组件。AGENTS.md 后续另行提供。

## 快速入口

| 你想… | 看这里 |
|---|---|
| **了解工程全貌（进程/运行/目录/构建）** | [docs/工程总览.md](docs/工程总览.md) — 工程实现主入口 |
| **了解项目与文档导航** | [docs/README.md](docs/README.md) — 项目概览 + 关键约束 + 全库索引 |
| **改视觉风格（配色 / 字体 / 形态 / 并行分工）** | [design/CMF.md](design/CMF.md) — 单一令牌源头 + 两端生成物 + 校验器 + 所有权边界 |
| **接入 QQ（SnowLuma）** | [docs/setup_snowluma.md](docs/setup_snowluma.md) — WebUI 配置步骤 + 自检 + 排错 |
| **组件版本与独立更新** | [updater/README.md](updater/README.md) — 更新中心、程序包、独立更新源、安装与恢复 |
| 查某个模块的 API | [docs/api/](docs/api/README.md) — 按代码包 1:1 对应的 API 参考 |
| 查已完成 / 待办 / 现象 / 远景 | [docs/已完成.md](docs/已完成.md) · [docs/待办.md](docs/待办.md) · [docs/现象观察.md](docs/现象观察.md) · [docs/未来改进.md](docs/未来改进.md) |
| 跑测试 | [docs/测试系统使用说明.md](docs/测试系统使用说明.md) |

## 目录结构（2026-10 实况）

```
main.py               FastAPI 入口（lifespan 启动 SystemCore；本身零业务路由，默认 5000）
manager_service.py    常驻管理服务（唯一进程控制者，127.0.0.1:49990；冻结为 manager.exe）
manager.py            管家 CLI（向管理服务发命令）
manager_core.py       手动进程管理原语（串行启停、被动退出检测，不自动重启）
paths.py              统一路径解析（app_root / data_root / log_root / resource_root）
core/                 SystemCore + ServiceRegistry 生命周期、supervisor 进程规格
adapt/                外部适配层：web(前端 5002)、onebot(NapCat 5001)、出站路由
memory/               消息入库与游标消费（chat_history.db）
brain/
  att/                注意力：station 仲裁 + 7 类 truck（注册表）+ proactive 后台循环
  pfc/                决策层（PFCA）与反思（reflection）
  chat/               对话代理（conversation / social_chat）
  amy/                情绪系统（EmotionalInterface 统一门面，DA/NE 双轴）
  hc/                 全局状态 + SQLite 长期记忆库（memory.db）+ 临时记忆
  base/               HeavyCore / HeavySession / LightweightAgent 等框架基类
utils/                日志 / 错误处理 / LLM 客户端 / 历史管理等共享服务
launcher/             WPF 桌面启动管理器（C#，net8.0-windows；构建产物在 launcher/bin/Release/net8.0-windows/）
SnowLuma/             QQ 登录工具（NapCat 系 OneBot 实现，含 node 运行时）
web_frontend/         Vue3 前端（WS 契约权威定义在 src/types.ts）
tests/                冒烟测试 + 预设测试环境
scripts/              迁移与校验脚本（check_api_docs.py 文档漂移校验、assemble_dist.py 打包）
design/               视觉体系：单一令牌源头 tokens.json + 生成器 + 三个校验器 + 规范（见 design/CMF.md）
dist/ build/          PyInstaller 构建产物与便携发布包（见 docs/工程总览.md 第三节）
docs/                 文档（工程总览 + 入口索引 + 四份状态文档 + api/ 活文档 + 指南）
```

## 端口

| 端口 | 组件 |
|---|---|
| 5000 | main.py FastAPI（仅承载 lifespan，无业务路由） |
| 5001 | OneBot v11 反向 WS 服务端（SnowLuma/NapCat 连入） |
| 5002 | WebChannel：对前端的全部 HTTP + WS 接口 |
| 5173 | web_frontend Vite 开发服（代理到 5002） |
| 49990 | manager_service 管理服务 REST（仅本机） |

## 启动

```bash
python manager.py                # 管家 CLI：Core/SnowLuma 启停、状态、日志（status/start/stop/restart/logs）
python main.py --dev             # 后端完整版（热重载；生产用 python main.py）
python scripts/web_only.py       # 不开大脑，只起前端与查看 API（仍提供部分编辑接口，非强制只读；--port 可换端口）
cd web_frontend && npm run dev   # 前端开发服；非开发时可直接开 5002（后端伺服 dist）
# 图形化：运行 dist/Abyss/AbyssLauncher.exe（WPF 启动器，自动拉起管理服务）
```

运行/打包/日志/配置位置的完整说明见 [docs/工程总览.md](docs/工程总览.md)。

## Web 系统设置与模型接入

网页顶部「系统」打开设置面板，默认展示 `common`、`embed` 两张基础接入卡片。
点击「编辑」填写 API 地址、模型名称和 API Key；已有密钥不回显，密钥输入框留空
表示保留，只有点击「清空密钥」或「全部使用 common」才会在保存时清除覆盖值。

`conversation`、`persona`、`vision`、`pro` 位于「按任务单独配置」。可选任务的模型名、
地址、密钥逐字段继承 common；pro 只覆盖模型名称。修改 common 后，未覆盖的字段
会随之变化。已有显式配置仍优先，可一键恢复使用 common。

embed 的模型名称独立配置，用于向量嵌入；输出需兼容现有 2048 维索引。若视觉任务
继承 common，common 应支持图片输入。所有卡片显示的是配置预览，「已配置」表示有
密钥配置，不代表已通过远端连接测试。

保存只提交修改字段，保存失败保留草稿，关闭面板前提示未保存的修改。LLM 配置需
重启 Core 后生效；新增的继承规则需使用包含本次源码修改的 Core，配置预览及 pro
编辑接口需使用更新后的 manager。冻结版需重新构建对应程序，刷新前端不会替换 exe。

## 平台接入（SnowLuma / 通用桥接）

通用平台桥接的阶段性实现及与微信侧的分工见
[接入契约 v0](design/bridge_contract_v0.md) 和
[转交 Qwen 的修改清单](design/qwen_bridge_changes.md)。源码已将桥接客户端接入独立
ingress、按会话来源发送的路由及「系统 → 平台接入」页面；通用桥接默认关闭，第一阶段
仅支持微信私聊/群聊文字。需使用符合契约的桥，不兼容缺少鉴权、缓存身份或持久幂等的旧桥。

页面可分别开启 SnowLuma 与通用桥接，填写桥接地址/令牌、只读检查连接、保存后按需
应用配置。令牌留空保留原值，GET 不回传明文；应用由管理服务依次停止 Core、重启
ingress、启动 Core。管理控制台的接入配置与检查不依赖 Core 在线。

消息按明确来源回送，不再由发送工具硬编码 QQ。桥接会话使用 `wechat_private_…` /
`wechat_group_…` 并隔离连接与缓存代次；结果不明不自动重发，待确认回执不冒充消息 ID。
当前仍保留旧 QQ 会话 ID，历史前缀迁移尚未执行；只读检查脚本
`python scripts/audit_session_prefixes.py` 不输出联系人或消息内容，也没有 apply 模式。

模拟验收：`scripts/acceptance_bridge_contract.py`、`scripts/acceptance_bridge_ingress.py`、
`scripts/acceptance_platform_api.py`，均不访问真实微信。冻结版需要重新构建 manager、
ingress、Core；刷新前端不会替换这些程序。真实微信联调及历史迁移需后续单独验收。

## PFCA 与反思系统的人格补充设定

在「记忆 → 人格 → 系统人格」中分别编辑 **PFCA** 与 **反思系统**。
两项默认留空；用户填写的正文直接追加到各自系统提示词末尾，下一次模型调用即读取
最新内容。清空正文可恢复为原始系统提示词，不覆盖程序内置提示词。

这两项只在人工页面展示，不加入内心人格注册表、候选清单或 LLM 工具。LLM 只能
看到追加后的正文，无法通过人格查询/修改工具发现或编辑这两项设置。数据独立保存在
数据目录的 `user_settings/system_personas.json`，修改旧内容前自动留档到同目录的
`history/`；该目录属于用户数据，不随程序发布。

## PFCA 效益评价

收尾按实际效益结算效能感，每轮只选择一档，不以任务完成与否代替判断。

| 评价 | 分数 |
|---|---:|
| 对方明确说自己有用/产生了实际价值/明确使对方收益 | +10 |
| 可能产生收益 | 0 |
| 结果不满 | -5 |
| 任务延期 | -10 |
| 产生负面收益 | -20 |
| 帮倒忙 | -30 |

正向只有一档，且必须由对方明确表达：对方说自己有用、已产生可指认的实际价值，或明确使对方收益。把话说得体面、对话顺利、自己长了见识、任务清单被推进，都不构成 +10 的依据；只有迹象而说不清收益的，选“可能产生收益 0”。负向按后果分层：让对方不满 -5、任务被延期或推迟 -10、产生负面收益 -20、帮倒忙 -30。自身收获不能抵消对外造成的损害。

评价按次结算，目前没有针对同一事件的自动撤销、更正或定向抵消机制。同一件事分多轮收尾时，后一次必须基于上次之后新出现的证据独立选档；同一件事换个说法重复评价只能选 0 及以下。“可能产生收益 0”只记评价结果、不改变数值。历史裁剪、想法词条到期或删除不会回退评分。社交欠账另有正向评价抵消旧欠账的机制，不适用于效益评价。旧记录与既有分数保留，不按新表重新结算。

## 统一维护工具

maintenance_action 仅分发给反思系统，严格对应四种状态：效能感100申请维护、为0尝试自修复；归属感为0情绪宣泄、100发布关系改进任务。除申请维护外，均自动交给 Quest 登记待办。登记成功后仅对应值重置为50。旧的申请审批链、48小时间隔、近期成功次数条件和+30保底补偿已移除。

AMY 结束评价词表区分正面与负面分区。负面收尾填写的 failure_reason 在本地缓存；维护申请发布时将当前整批原因归入该申请并清空缓存，之后的新原因属于下一批。前端维护页按申请展示其原因。PFCA 仅保留 failure_reason，不再单独填写 task_reason 或 evaluation_reason；归属和效益变动说明采用事件标题。

经验条目独立记录检索次数、上次人工维护时间、人工维护次数。查看、检索与申请本身不计人工维护；人编辑或明确记录一次维护才计数。见闻录继续独立保存，不与失败原因混合。
