# AGENTS.md — N_Touch（Abyss）安装与使用手册

> 读者：接手帮忙的另一个 AI 助手。
> 对象：完全不懂技术的普通用户（可以按“傻瓜”理解，但不是贬义）。
> 目标：让用户用最少的点击完成安装、配置、更新和 QQ 接入；出问题时你能快速定位。

**工作区目录约定（2026-11-03）**

根目录只保留 `app / data / external / dev / cache / backups / private` 七个区域和入口说明。`app` 是运行版，`dev` 是开发工程；私人交接、参考图和实验项目位于 `private`。开发构建和隔离测试只写入 `cache`。旧的“常用版本”和 `dist/Abyss` 入口已撤除，日常使用根目录的 `启动弱链接.cmd` 或 `app/AbyssLauncher.exe`。

**Agent 修改与实际运行位置**

- 日常入口仍是 `app\AbyssLauncher.exe`，但管理服务、Core、消息接入均以组件内源码运行；不再使用独立的 manager/core/ingress EXE。
- 修改前先通过 `GET /v1/storage` 确认实际源码和数据位置。`dev` 是开发源，修改仓库不会自动替换已经安装的运行源码。
- 运行中的 Core 源码在 `app\components\core\source`，管理服务源码在 `app\components\manager\source`。
- 可先调用 `POST /v1/updates/source-backup`（component 为 core 或 manager）保留当前代码，再直接修改。后端修改后重启对应服务；前端修改后由 Agent 构建对应组件的 `web_frontend`。
- 历史数据已迁入工作区 `data`。SnowLuma 原安装位置由 `data\manager\installations.json` 登记，内部存储遵循上游。
- 一键启动框架：实际脚本为 `app/components/manager/source/startup_plan.py`，执行器为同目录 `startup_workflow.py`；开发副本在 `dev`。外部 AI 直接编辑脚本，说明见 `dev/docs/STARTUP.md`。管理接口为 `GET /v1/startup` 和 `POST /v1/startup/{start|continue|retry|skip|cancel|enable|stop|reload}`，沿用管理令牌。打开 EXE 只展示界面，点击「一键启动」才执行已勾选链路；Pi 和 QQ 独立，失败可跳过或单独继续。改脚本可暂停流程后调用 reload，无须重新打包 EXE；改框架/进程控制仍需重启管理服务。

**沟通原则**

- 能用界面点出来的，绝不让用户敲命令、改代码、装 Python。
- 一次只给一步操作，写清楚按钮名称和位置；等用户回报结果再给下一步。
- 不要让用户把 API Key、管理令牌贴进聊天。配置都在 `data\.env.dev`，看路径即可。
- 需要诊断材料时，让用户走图形界面拿：左侧「日志」→ 选择来源 →「复制」或「打开日志目录」。
- 判断版本以启动器界面显示的为准，不要凭文档里的数字猜。

---

## 1. 先判现状（30 秒）

安装版按用途分区，都在当前用户下，不需要管理员权限：

| 内容 | 路径 |
|---|---|
| 程序 | `%LOCALAPPDATA%\Abyss\app` |
| 数据（记忆、配置、日志） | `%LOCALAPPDATA%\Abyss\data` |
| 第三方默认安装区 | `%LOCALAPPDATA%\Abyss\external` |
| 下载与安装暂存 | `%LOCALAPPDATA%\Abyss\cache` |
| 源码与更新备份 | `%LOCALAPPDATA%\Abyss\backups` |

已有旧数据目录、SnowLuma 安装位置和更新任务保持原位兼容。外部程序的数据布局由上游管理，不强制其写入 Abyss 的数据目录。完整规则见 `dev/docs/STORAGE.md`。

`app` 里应能看到唯一的打包业务程序 `AbyssLauncher.exe`、`components\manager\source`、`components\core\source` 和 `.versions\`。各源码程序使用自带解释器，更新事务也由启动器执行。

**注意**：源码仓库里的日常运行目录是 `app`；开发源码位于 `dev`，构建输出位于 `cache\builds`，`cache\builds\public-*\` 等是构建/测试产物，不是给朋友用的安装环境。不要把开发目录当成用户安装。

判断是否已经在运行（AI 可执行，先征得用户同意）：

```powershell
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object LocalPort -in 5000,5001,5002,49990 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

有 49990 说明管理服务（启动器后台）在跑；有 5002 说明 Core 的网页在跑。

---

## 2. 安装（常规：联网）

1. 用户只下载一个文件：`AbyssLauncher.exe`（GitHub Release 的唯一下载入口）。
2. 双击运行。首次运行会把管理服务和更新器释放到 `%LOCALAPPDATA%\Abyss\app`，并创建同级 `data`。
3. Windows 提示“Windows 已保护你的电脑”时：点「更多信息」→「仍要运行」。这是未签名程序的正常提示。
4. 启动器窗口出现后，左栏「总览与控制」里能看到组件清单。此时 **Core 显示“未安装”是正常的**——程序不会自动下载、不会自动安装。
5. 在 Core 行点一下展开，选择版本，点行尾的下载图标（向下的箭头）开始下载；准备好后再点同一个图标，这次是安装，会弹确认窗口。
6. 装好 Core 后点 Core 的启动按钮，会弹出「首次启动 Core」配置窗口（见第 4 节）。
7. SnowLuma（QQ）同理：在 SnowLuma 行点下载图标。它从官方 GitHub Releases 获取，需要能访问 GitHub。

环境要求：Windows 10/11 64 位、能上网（GitHub 与模型 API 域名）、Windows 自带 WebView2 运行时（Win11 自带；Win10 通常随 Edge 已安装，网页空白时再考虑补装）。**不需要 Python、Node、Git、管理员权限。** 首次运行时 Windows 防火墙若弹窗，选择“允许访问”即可。

---

## 3. 本地/离线安装包（U 盘拷贝）

适用于用户拿到了别人打好的发布目录，或 GitHub 下载太慢。

**要拷贝给用户的内容**（保持目录关系）：

```
AbyssLauncher.exe            ← 发布目录 download\ 里的那一个
packages\                    ← 整个文件夹：*.zip 与同名 *.json 必须在一起
```

**安装步骤**

1. 双击 `AbyssLauncher.exe`（首次会释放到 `%LOCALAPPDATA%\Abyss\app`）。
2. 左栏「总览与控制」→ 点 Core 那一行展开（行尾有展开箭头）。
3. 展开区里点「导入本地包」，选择 `packages\core-<版本>.zip`。
   - **不要解压 zip**；同目录必须有同名的 `.json` 清单，否则校验会失败。
4. 导入成功后，版本下拉会落在刚导入的版本上；点行尾下载图标完成“准备”，再点一次执行安装并确认。
5. SnowLuma 没有随包提供时，仍需联网从官方源安装，或由维护者另行提供带 JSON 的本地包。

---

## 4. 首次启动 Core：身份 + 模型接入

点 Core 的启动按钮时自动弹出，标题为「首次启动 Core」。字段：

| 字段 | 填什么 |
|---|---|
| 你的 QQ 号 | 使用者本人的 QQ（主人）。私聊白名单、审批都以它为准 |
| 你的称呼 | 机器人对你的称呼，默认“主人” |
| 助手登录的 QQ 号 | 机器人自己的 QQ，必须与 SnowLuma 登录的号一致 |
| 助手名称 | 机器人自称，默认“助手” |
| 模型 API 地址 | OpenAI 兼容地址；默认火山方舟 `https://ark.cn-beijing.volces.com/api/plan/v3` |
| 模型名称 | 默认 `DeepSeek-V4.1-Flash`，必须与供应商给的模型 ID 完全一致 |
| API Key | 密钥。**支持 Ctrl+V 和右键「粘贴」**；粘贴时自动去掉换行和首尾空格 |

- 两个 QQ 号必须是纯数字，否则保存会报错。
- 点「稍后设置」会取消本次启动，不保存；以后每次点启动都会重新弹这个窗口，直到 QQ 号和 API Key 都有值。
- 保存后写入 `data\.env.dev`，更新 Core 不会丢。

**以后要改身份信息**：目前没有网页入口。做法是：停止 Core → 用记事本打开 `data\.env.dev`，删除 `MASTER_ID=`（或 `ASSISTANT_QQ=`）那一行 → 再点启动，首次配置窗口会重新出现，其余已填字段会自动带出来。API Key 留空即保留原值。

---

## 5. 模型接入（网页「系统」→「模型接入」）

启动器左栏「管理页面」打开的就是管理控制台（Core 停止时也能打开）。点页面顶部「系统」→ 左侧「模型接入」。

面板有 6 张卡片：

| 卡片 | 用途 | 地址/模型/密钥字段 |
|---|---|---|
| common | 所有未单独配置的任务都用它 | `FLASH_BASE_URL` / `LLM_FLASH` / `FLASH_API_KEY` |
| embed | 记忆检索用的向量嵌入 | `VECTOR_BASE_URL` / `LLM_EMBEDDING` / `VECTOR_API_KEY` |
| conversation | 会话与社交聊天 | `CONVERSATION_BASE_URL` / `LLM_CONVERSATION` / `CONVERSATION_API_KEY` |
| persona | 内心人格 | `PERSONA_BASE_URL` / `LLM_PERSONA` / `PERSONA_API_KEY` |
| vision | 图片理解 | `VISION_BASE_URL` / `LLM_VISION` / `VISION_API_KEY` |
| pro | 高阶任务 | 只覆盖 `LLM_PRO`，地址与 Key 始终用 common |

规则：

- 可选卡片留空 = 继承 common；填了才覆盖。保存只提交改动字段。
- 密钥保存后**不回显**。输入框留空表示保留；点「清空密钥」再保存才会清除覆盖值。
- 每张卡片右上角有「**测试连接**」：用当前填写的地址/模型/Key 发一次最小请求（20 秒超时）。没填 Key 时服务端会用已保存的 Key。成功提示“连接成功 · 模型已响应/嵌入成功”；失败给出具体原因。
  - 测试需要 **Core 正在运行**（Core 停止时提示“Core 暂时无法连接”）。
  - 测试会产生一次极小的真实调用费用；embed 组会真的生成一条向量。
  - 测试通过只代表地址、Key、模型名基本正确，不代表图片能力或向量维度正确。
- 保存后页面提示“配置已保存，重启 Core 后生效”。点面板底部「重启 Core」，或回启动器停止再启动。**不重启不会生效。**

**向量嵌入的硬约束**：默认 `doubao-embedding-vision`，输出 2048 维，系统按 2048 建索引。换成其它维度的模型（例如 1536 维）不会报错，但向量比对会静默失效。

- 新装、还没产生记忆时，若要换嵌入供应商，应先确认维度是 2048；不是就不要换，或找维护者改 `config/settings.py` 的 `VECTOR_DIMENSIONS` 后清空旧向量。
- 不要在已有记忆数据上随意换嵌入模型。

**API 地址写法**：填供应商文档里给 OpenAI SDK 的 base URL（通常以 `/v1` 结尾）。系统会在其后拼 `/chat/completions`（embed 组会拼 `/embeddings` 或火山的多模态接口）。地址多了或少了一层 `/v1` 是最常见的 404 原因。

---

## 6. 更新

- 启动器启动时、以及每 6 小时自动**检查**版本；检查不会自动下载或安装。
- 手动检查：总览页「检查全部版本」；单个组件用行右侧版本下拉旁的刷新图标拉取可选版本（可加载历史版本/降级）。
- 操作顺序：选版本 → 点行尾下载图标（下载/准备本地包）→ 同一个图标变为“安装” → 弹窗确认。
- 下载期间组件可以继续运行；安装会按影响范围暂停相关服务。

**Core 的两道确认**：下载和安装各自有一个确认窗口，显示版本说明、破坏性改动、最低组件版本要求、数据迁移说明。

- **检测到本地改过 Core 源码时，安装会被阻止**。不要教用户绕过。正确做法：先把 `app\components\core\source` 整份复制备份；要更新就还原到官方基线，或在独立目录装新版做对比合并。
- Core 展开区的「查看源码改动」「备份源码」用于保留 Agent 直接修改的程序；无需编写独立扩展代码。源码快照在 `backups\core-source\`，备份不会解除本地修改保护。
- 新安装任务会把旧程序备份到 `backups\updates\<任务号>\backup\`，旧的 `data\manager\updates\jobs\` 继续识别，版本下拉里也能选回这些本机备份。
- **程序可以回退，用户数据不会跟着回退**。记忆库、配置、人格不会被旧版本覆盖；需要恢复数据只能手动按备份处理。
- 源码型 Core 安装会停止 Core 与消息接入，但保留 SnowLuma 登录。
- 更新启动器会关闭并重开窗口；后台服务继续运行。更新管理服务会短暂停掉它管着的进程，之后恢复原运行状态。
- 更新完 Core 必须重新启动 Core；只刷新网页不会加载新后端。

**不要**同时保留多份启动器去点不同目录的 EXE：会抢 5000/5001/5002/49990 端口。始终双击最开始那个下载入口；它每次都会转到已安装版。

---

## 7. 接入 QQ（SnowLuma）

原理：Core 在本机开一个 OneBot v11 WebSocket **服务端**（默认 `ws://127.0.0.1:5001/onebot/v11/ws`），SnowLuma 在自己的 WebUI 里加一条客户端连过来。

**A. 安装与启动**

1. 启动器「总览与控制」→ SnowLuma 行 → 下载（官方 GitHub 源）→ 安装 → 启动。
2. 启动后，左栏点「SnowLuma」入口，打开它的 WebUI（默认 `http://127.0.0.1:5099`）。
3. 在 WebUI 里登录要接入的 QQ 号（可能需要在手机 QQ 上确认）。**这个号就是首次配置里“助手登录的 QQ 号”。**

**B. 添加连接（全部在 SnowLuma WebUI 内完成）**

进入「网络配置」，新建一条 **WebSocket 客户端**（NapCat 系叫反向 WebSocket 连接）：

| 字段 | 值 |
|---|---|
| URL | `ws://127.0.0.1:5001/onebot/v11/ws` |
| accessToken | 与 Core「平台接入」里填写的一致。没改过时内置值是 `test-token-123`，建议在平台接入里改成自己的令牌，两边同步 |
| 消息格式 messageFormat | **Array** |
| reportSelfMessage | 关 |
| 心跳/重连等 | 保持默认 |

保存并启用。SnowLuma 会自动重连，先开谁无所谓。

**C. 查看和修改 Core 侧参数**

网页「系统」→「平台接入」→ SnowLuma 卡片：

- 开关：是否启用 QQ 接入（对应 `ONEBOT_ENABLED`）
- 监听端口 / 连接路径：默认 5001 / `/onebot/v11/ws`
- 访问令牌：留空保留。建议在这里重设一个自己的令牌并保存应用，然后在 SnowLuma 填同一个；内置默认值是 `test-token-123`
- 状态显示「已连接 / 等待 SnowLuma 连接 / 未启用 / 接入服务未连接」
- 改完保存后按提示应用；修改端口或令牌需要重启 Core

**D. 验证**

1. 「平台接入」状态变为「已连接」；或 Core 日志出现 `NapCat connected from 127.0.0.1`。
2. 用另一个 QQ 给助手号发私聊。
3. 打开网页「会话记录」，能看到消息入库；Core 的回复会经同一条 WS 发回 QQ。

**E. 预期行为（避免误判为故障）**

- 默认只允许对话代理对**主人私聊**发起对话。群聊或他人私聊需要到「记忆 → 社交实体」给对应群/角色勾选“允许 conversation”。
- 对外社交发送默认是 **approval 模式**：每条事件链第一次对外发送前，机器人会在主人会话里请求审批，主人回复“同意”后放行；同一链同一会话后续共享这次授权，链结束即失效。
- 关掉「平台接入」里的 SnowLuma 开关、重启 Core 后，Web 面板不受影响，只是不再收发 QQ。

---

## 8. 装完 / 更新完的验收清单

按顺序确认，全过才算完成：

1. 启动器「总览与控制」：Core 状态为运行中，版本号正确。
2. 「管理页面」能正常打开，不是空白页；标题栏刷新按钮可恢复。
3. 「系统 → 模型接入」：common 卡片显示“已配置”；点「测试连接」成功。
4. 改一句配置并保存，按提示重启 Core，配置仍在（密钥不回显属正常）。
5. 「系统 → 平台接入」：SnowLuma 显示「已连接」。
6. 「会话记录」里能看到一条真实新消息（QQ 或网页测试会话）。

---

## 9. 排错速查

| 现象 | 先查 | 处理 |
|---|---|---|
| 双击 EXE 没反应 | 杀毒/安全软件、SmartScreen | 允许运行；加入白名单；确认用的是 `%LOCALAPPDATA%\Abyss\app\AbyssLauncher.exe` |
| 启动器能开，网页空白 | Core 是否运行；WebView2 | 点标题栏刷新；启动 Core；必要时安装 WebView2 Runtime |
| Core 一直“未安装” | 是否点过下载/导入 | 在线下载，或「导入本地包」（zip+同名 json） |
| Core 启动失败 | 左侧「日志」→ Core / 启动输出 | 常见：端口被占（5000/5001/5002）、旧 Core 缺依赖 → 更新 Core |
| 报 `WinError 10013` 绑定 5001 失败 | 端口占用或系统保留段 | 先关掉重复实例/占用 5001 的程序再启动；仍是 10013 时重启电脑重试。仅源码仓库用户可运行 `dev\scripts\repair_tcp_port_range.ps1` 修复系统保留端口 |
| 首次配置窗口不出现 | 已存在 `MASTER_ID` 与 API Key | 要改身份就删 `data\.env.dev` 里对应行再启动 |
| 测试连接 401/404 | 地址、模型名、Key | 核对 base URL（是否含 `/v1`）与模型 ID；确认 Key 属于该接入点 |
| 测试连接超时 | 网络/代理；Core 是否运行 | 换网络或取消代理；确认 Core 在跑 |
| 向量比对报错/检索失效 | Core 版本；嵌入维度 | 更新到包含最新修复的 Core；维度必须 2048 |
| SnowLuma 一直重连 | URL、令牌、Array | 三项逐字核对；改完重启 Core |
| SnowLuma 401 | 令牌不一致 | 平台接入与 SnowLuma WebUI 两侧同步 |
| QQ 连上但群里不回 | 行为设计 | 到「记忆 → 社交实体」允许 conversation；对外发送需主人审批 |
| 更新提示“检测到本地修改” | 程序源码被改过 | 不要绕过；备份 source 后还原基线或在独立目录试新版 |
| 更新后记忆/配置不见了 | 数据目录是否被换 | 数据在 `data`；确认没有装到另一份目录或换了 `ABYSS_DATA_DIR` |
| 浏览器单独打开管理页要令牌 | 需要管理令牌 | 用 `data\manager\service_token.txt` 的内容登录；从启动器打开则免登录 |
| 更新下载很慢/失败 | GitHub 连通性 | 重试；或改用本地包导入 |

**收集诊断信息时让用户提供**：① 各组件版本号（截图总览页）② 出错时的提示原文/截图 ③ 日志（日志页“复制”或“打开日志目录”后打包）。不要索取密钥。

---

## 10. 文件、端口与日志

| 项目 | 位置 |
|---|---|
| 程序根 | `%LOCALAPPDATA%\Abyss\app` |
| 数据根 | `%LOCALAPPDATA%\Abyss\data` |
| 部署配置（身份/端口/密钥） | `data\.env.dev` |
| 记忆与会话库 | `data\memory\` |
| 日志 | `data\logs\`（Core 的 `app_*.log`、启动输出、SnowLuma、消息接入） |
| 管理令牌 | `data\manager\service_token.txt` |
| 更新源设置 | `data\manager\update_sources.json` |
| 新更新备份 / 旧任务兼容 | `backups\updates\` / `data\manager\updates\jobs\` |
| 手动源码备份 | `backups\core-source\` |
| 更新下载与解压暂存 | `cache\updates\` |
| 启动器连接入口 | `data\launcher\connections.json` |
| Core 源码 / 运行环境 | `app\components\core\source` / `app\components\core\runtime` |
| 已安装版本记录 | `app\.versions\*.json` |

| 端口 | 用途 |
|---|---|
| 49990 | 管理服务 + 管理网页（仅本机） |
| 5000 | Core 主进程（无业务路由） |
| 5001 | OneBot WebSocket（SnowLuma 连入） |
| 5002 | Core 网页/接口 |
| 5099 | SnowLuma 自己的 WebUI（默认） |

日志入口优先走界面：左栏「日志」→ 选 Core / SnowLuma / 消息接入 / 管理服务 →「刷新」「复制」「打开日志目录」。

---

## 11. 绝对不要做的事

1. 不要让用户安装 Python、Node、pip 依赖，或手动跑 `main.py`。
2. 不要删除、移动 `data` 目录，也不要在里面放临时文件。记忆、人格、账号、密钥都在这里。
3. 不要把 Core 的 zip 解压到程序目录或数据目录。
4. 不要同时运行两份启动器/旧目录的 EXE。
5. 不要在有用户数据时更换嵌入模型或改向量维度。
6. 不要教用户绕过“本地修改检测”去覆盖安装。
7. 不要把 `.env.dev`、令牌、API Key 的内容发到聊天或截图里。
8. 不要用管理员权限运行；不要改系统环境变量来“修”问题（除非文档明确要求）。

---

## 12. 维护者备注（不面向普通用户）

- 源码开发模式：先进入 `dev`，再运行 `python manager.py` / `python main.py --dev`；仓库模式永远不会被组件安装覆盖。
- 公开发布构建：在 `dev` 执行 `python scripts/build_public_release.py --output ../cache/builds/<新目录> --feed-base-url ... --package-base-url ... --metadata releases/core-<版本>.json`，产物是 `download\AbyssLauncher.exe` + `packages\`（ZIP 与同名 JSON 必须成对，先传包后传清单）。
- CI 发布：改 `dev/component_versions.json` 与 `dev/releases/core-*.json`，推 `v*` 标签，`.github/workflows/release.yml` 构建并上传 Release。
- 组件版本、更新源和安装边界的细节见 `dev/updater/README.md`；QQ 接入的完整参数与排错见本文件第 7 节与 `dev/config/adapt_settings.py` 注释。
