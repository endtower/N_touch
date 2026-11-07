# 文件存储与源码修改约定

唯一打包的自有业务程序为 `AbyssLauncher.exe`。管理服务、Core 和消息接入以源码运行，更新事务由同一个启动器的无界面模式执行。Python/Node 解释器仍随项目提供，它们不属于业务程序打包。

用户通过启动器和各项目的 WebUI 使用程序；外部 Agent 可以直接修改实际运行的源码。本项目不引入独立的扩展代码加载体系。

## 标准安装布局

默认布局根为 `%LOCALAPPDATA%/Abyss`。

| 区域 | 内容 | 所有权与更新规则 |
|---|---|---|
| `app/` | 启动器，以及管理服务、Core 的源码和匹配运行环境 | 按组件安装与更新；本地源码改动阻止直接覆盖 |
| `data/` | Abyss 配置、记忆、人格、会话、附件、日志、启动器设置、更新控制状态 | 用户数据，程序替换和回退不覆盖 |
| `external/<项目>/` | 第三方程序默认安装位置 | 内部配置、日志、登录状态和系统目录使用方式由上游决定 |
| `cache/updates/` | 下载暂存、准备安装的 payload、保留的离线版本包、临时隔离验收 | 与个人数据和备份分开；暂不提供自动清理，不能删除正在安装或唯一的离线包 |
| `backups/updates/<任务>/` | 更新前程序备份、Core 状态备份、任务日志与恢复材料 | 私有；保留旧版本选择与中断恢复能力 |
| `backups/core-source/<时间与标识>/` | 用户当前源码的独立快照与安装基线记录 | 包含 Agent 新增和修改的文件；不回写或重置源码 |

启动脚本是用户数据，不是程序：`data/manager/startup_plan.py` 与 `data/manager/tools/` 归用户（或他请的 Agent）所有，随 `data/` 一起不受组件更新影响，也不进版本库。`app/components/manager/source/startup_plan.py` 只是随包分发的出厂最简版，两层同名文件按「数据根优先、程序目录兜底」取用。执行器 `startup_workflow.py` 属于程序，随组件更新。

Core 的实际源码为 `app/components/core/source`，运行环境为 `app/components/core/runtime`。管理服务对应 `app/components/manager/source` 与 `runtime`；不再生成 `manager.exe/core.exe/ingress.exe/AbyssUpdater.exe`。本机日常程序根为 `app`，通过 `abyss-storage-dir.txt` 指向工作区布局根。不要把数据库、个人配置或运行产物写回源码目录。前端编译资源属于程序。

开发工程位于 `dev`，通过 `dev/abyss-storage-dir.txt` 将布局根指向上一级，复用同一份 `data/cache/external/backups`。其他未配置描述文件的独立源码目录仍以目录自身为布局根。受管 Core 内的 `paths.py` 会识别 `components/core/source`，与管理服务使用同一个标准安装布局根。

## 旧布局兼容

数据位置按以下顺序解析：

1. 当前进程的 `ABYSS_DATA_DIR`。
2. 程序旁的 `abyss-data-dir.txt`；相对路径以程序目录为基准，空内容或不存在的目标报错。
3. 程序目录存在旧配置、记忆库、消息库或管理令牌等标志时，继续使用该目录。
4. 否则使用布局根下的 `data`。

新默认不自动迁移旧文件，也不改变现有实例的数据根。旧工作区继续运行；真正迁移需要处理停止写入、一致性复制、路径切换和恢复。Python 与启动器的默认规则需同步维护。

配置只读取当前数据根中的 `.env.dev`；显式选择另一数据根后，不再回退读取程序目录里另一份账号或密钥。旧布局的数据根本来就是程序目录，因此仍可原位使用。

新更新任务仍将控制状态保存在 `data/manager/updates`，将 payload 与保留包写到 `cache/updates`，将任务备份写到 `backups/updates`。旧的 `data/manager/updates/jobs`、`packages`、已准备 payload 继续识别，不移动或清空；旧任务中的绝对路径继续用于恢复。

## 第三方程序：管理安装与启动，存储遵循上游

SnowLuma 安装位置解析顺序：显式 `ABYSS_SNOWLUMA_DIR` → `data/manager/installations.json` 中登记的位置 → 已存在的 `<data>/SnowLuma` → 已存在的 `<app>/SnowLuma` → `<布局根>/external/snowluma`。

仅最后一项是新安装默认位置。旧安装仍按已登记位置读取。本机已将整个安装目录校验后迁入 `external/snowluma`；内部相对布局和文件内容保持原样，登记位置随之更新。启动器提供进程管理和 WebUI 入口，不向第三方注入一个统一的个人数据目录，也不接管第三方在系统其他位置的数据。

更新仍按明确的程序文件白名单执行，保留 SnowLuma 的 `config/data/logs`，不能先清空整个安装目录再解压。备份其安装目录也不能宣称备份了全部 QQ 账号资料。

## 直接修改源码与更新

用户可以让其他 Agent 直接修改源码，不需要实现专用扩展接口。Core 和管理服务的源码相对安装收据的新增、删除和修改都需要保护，包括新加的 JS、JSON 等非发行白名单文件。缓存、字节码、Git 元数据和 `node_modules` 不作为源码修改处理。

未修改源码时可按正常确认流程更新。已经修改时保留当前版本并阻止覆盖；可下载官方新版供 Agent 在副本中对比合并。备份只是保存改动，不会解除修改保护，也不会把用户改动自动登记成官方基线。

启动器的 Core 展开区提供「查看源码改动」「备份源码」。这些按钮用于已经安装的源码 Core；仓库开发代码仍由版本控制保留。源码备份记录每个文件的摘要，复制前后检查内容变化，只有 `snapshot.json` 的 `state=complete` 表示完整快照。未完成的备份目录保留已复制内容供排查，不能当作成功快照。

源码快照不包含运行环境、个人数据、Git 历史或依赖缓存。它可能包含用户自行写进源码的私有内容，因此也属于私有备份。

## Core 更新数据备份的范围

更新器停止相关写入进程后备份配置、人格、情绪状态、笔记、JSON 历史与数据库。Core 和消息接入共同停止时，包含 `memory/chat_history` 中的消息库、接入收件箱、桥接状态库及 SQLite WAL/SHM 文件，不再只匹配 `memory` 顶层。

`data-backup/backup.json` 记录文件清单、摘要、是否包含消息库和备份范围。此备份不复制媒体附件、第三方账号数据，也不是全量数据迁移备份。程序回退不自动还原数据库，避免覆盖更新后产生的新消息。

## Agent 管理入口

以下接口都要求本机管理令牌，通过现有鉴权机制使用；不要将令牌或 API Key 放入聊天。

| 接口 | 用途 |
|---|---|
| `GET /v1/storage` | 取得程序、数据、缓存、备份、第三方安装位置及所有权规则，不返回配置值 |
| `POST /v1/updates/source`，请求 `{"component":"core"}`（也支持 `manager`） | 取得源码路径、备份位置及本地修改清单 |
| `POST /v1/updates/source-backup`，请求 `{"component":"core"}`（也支持 `manager`） | 保存当前源码快照，返回备份目录与文件数，不停止服务、不替换源码 |

已有安装、配置、状态、日志、启停和更新接口继续使用。页面入口、运行进程和安装组件仍是不同对象；新增第三方项目管理能力应围绕其安装和启动约定实现。

## 本机迁移

`scripts/deploy_source_layout.py` 默认预览；执行时先检查没有相关写入进程和进行中的更新任务，再完整备份并校验要迁移的文件，最后迁移到 `data`。旧程序整目录保存到 `backups/migrations/<时间>/program-before`，旧更新包与任务移入 `backups/legacy-updates`，历史前端运行残留单独保存在 `data/legacy`。会话文本、人格内容、数据库结构与嵌入模型保持原样。迁移清单记录每个文件的原位置、目标和摘要，失败时恢复已移动文件。

## 工作区顶层分区（2026-11-03）

根目录为 `app / data / external / dev / cache / backups / private`。根 `README.md` 是日常导航，`AGENTS.md` 是接手说明，`启动Abyss.cmd` 打开 `app/AbyssLauncher.exe`。

开发源码整体迁入 `dev/`，原有包之间的相对关系保留。`cache/builds/` 保存构建输出，`cache/tests/` 保存隔离验收，`cache/tools/` 保存开发工具链。旧构建、诊断和目录骨架归档到对应缓存子目录，完整实验项目、私人参考和历史交接保存到 `private/`。不使用根目录兼容链接，也不在根目录重新创建旧的源码目录。

`data/` 本轮不搬动；只更新管理器的第三方安装登记。安装收据只修正目标位置，文件摘要与源码基线不重建。开发和运行源码不会自动同步。

本次搬迁清单和程序完整备份位于 `backups/workspace-layout/<时间>/`，`migration.json` 记录每项位置及校验摘要。`relocated-awaiting-path-repairs` 仅表示文件搬完，只有完成路径修正与验收才标记为 `complete`。不要在已整理的工作区重复运行一次性迁移工具。

### 版本目录与历史备份的迁移

`data/manager/updates/catalog/` 是正在使用的版本目录，迁移时不能只归档到备份区。历史包和更新任务可以保存在 `backups/legacy-updates/`，但被管理器读取的历史安装收据也要同步登记新的目标位置；文件摘要和备份程序内容保持不变。

已发生遗漏时，维护脚本 `dev/scripts/repair_update_history.py` 可恢复归档目录中的版本清单、合并同源版本，并仅修正已完成历史任务中已知旧安装位置的收据。它保留旧元数据副本，不修改数据库、程序包、源码基线或备份文件；更新任务进行中拒绝执行，不覆盖用户主动更换的更新源。结果和原始元数据保存在 `backups/update-history-repair/`。管理服务每次读取版本列表时重新读取这些记录，修复本身不需要重启服务。

### 旧身份配置

旧私人版本可能把 QQ 号、主人称呼和助手名称直接写在 `config/role_settings.py`，而没有写入 `.env.dev`。源码运行时能使用这些旧值，但启动器的首次配置检查读取的是数据根中的环境配置，因此迁移时必须将已有身份字段同步到 `data/.env.dev`。只补充缺失字段，保留已有环境配置和密钥；不能把公开模板的占位账号当成用户身份。不能为绕过配置窗口而跳过真实身份校验。

本机已从现用 Core 的 `RoleConfig` 核对并补齐这五个身份字段，旧环境配置保存在 `backups/config-migrations/`。配置检查每次启动 Core 时重新读取，无须重启管理服务；已经打开的首次配置窗口应取消后重新启动。
