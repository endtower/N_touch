# 源码组件安装与更新

当前只有 `AbyssLauncher.exe` 是打包的自有业务程序。管理服务、Core、消息接入以源码和受管 Python 运行环境运行。更新事务编译进启动器，通过 `AbyssLauncher.exe --update-job <任务> ...` 执行；不再另行发布 `manager.exe/core.exe/ingress.exe/AbyssUpdater.exe`。

Python、Node 等上游解释器仍以其原始形式随项目提供。第三方程序的数据布局由上游决定。

## 安装与存储

标准目录及所有权见 [STORAGE.md](../docs/STORAGE.md)。Core 在 `app/components/core/source`，管理服务在 `app/components/manager/source`，对应解释器在各自 `runtime` 中。消息接入与 Core 共用源码和运行环境，不单独更新。

首次下载的启动器携带管理服务源码和 Python 环境；校验后释放到用户安装目录，并生成管理服务安装收据。Core 与 SnowLuma 按需安装。

新下载与 payload 在 `cache/updates`；控制状态在 `data/manager/updates`；新任务、程序和状态备份在 `backups/updates`。源码独立快照在 `backups/core-source` 或 `backups/manager-source`。

本机日常程序根为工作区 `app`，由描述文件指向工作区布局根和 `data`。历史数据迁移工具先完整备份再校验移动，旧程序整份保存在迁移备份内，旧更新资料在 `backups/legacy-updates`。迁移不改变会话文本、人格内容、向量模型或数据库结构。

## 直接修改源码

用户可让 Agent 直接编辑实际运行的源码，无须编写独立扩展代码。修改前通过 `GET /v1/storage` 取得真实位置，可先调用源码备份接口。

Core 和管理服务的修改保护覆盖源码新增、删除和修改，包括非发行白名单的新文件。备份不会解除修改保护。仓库开发模式不会被组件安装覆盖。已采用源码布局的安装也不能被旧二进制组件包覆盖。

源码修改后应在副本中对比和合并官方新版。更新不会自动替用户判断修改是否兼容，也不会自动将用户修改登记为官方基线。

## 确认、备份和恢复

自动检查只读取版本清单，不自动安装。Core 下载和安装分别确认，确认与版本说明、兼容要求、当前文件及准备包绑定，十分钟内单次有效。

安装前先验证源码依赖，再按依赖顺序停止相关受管进程。Core 与消息接入共同停止时，状态备份覆盖嵌套的消息库、接入收件箱和桥接状态库及 SQLite WAL/SHM。清单记录实际文件与摘要。更新状态备份不含媒体附件和第三方账号资料，不等于全量迁移备份。

管理服务更新会暂停其持有的进程；换好源码后由同一个启动器的任务模式拉起新管理服务，再恢复先前运行的目标。启动器更新会等待旧窗口退出再替换并打开新窗口。文件事务保留日志和回滚材料；程序回退不自动覆盖用户数据库。

旧任务和旧安装包保留兼容读取能力，但旧二进制包不能覆盖当前源码式安装。中断任务必须恢复后才能继续操作受影响项目。

## 第三方边界

SnowLuma 安装位置按显式环境变量、已登记位置、旧目录、新默认 `external/snowluma` 解析。更新器与进程控制器共用同一解析函数。

不迁移 SnowLuma 内部配置、日志或账号资料，不强制其使用 Abyss 数据根。程序与数据混放时仍只替换明确允许的程序文件，保留 `config/data/logs`；不能清空安装目录后重新解压，也不能把安装目录备份描述成完整的 QQ 账号备份。

## 构建与本机部署

公开构建入口为 `scripts/build_public_release.py`，实际调用 `build_source_distribution.py`。它生成单 EXE 下载入口、源码 Core 包、源码管理服务包及启动器更新包；发布说明在 `releases/core-<版本>.json`。没有业务 PyInstaller 构建步骤。

`build_source_distribution.py --private` 仅用于本机迁移，保留本机 Python 配置源码，不生成公共更新包或带私人配置的公开引导包。

`deploy_source_layout.py` 默认预览，`--execute` 才执行备份、校验和迁移。`start_source_layout.py` 用于启动迁移后的运行版；桌面启动仍以`app/AbyssLauncher.exe` 为入口。

## 管理接口

所有接口仅在本机提供，并要求管理令牌；不得把令牌或 API Key 发到聊天。

| 接口 | 用途 |
|---|---|
| `GET /v1/storage` | 程序、数据、源码、缓存、备份、第三方位置及所有权 |
| `GET /v1/updates` | 组件版本、准备状态、旧包与备份、更新结果 |
| `POST /v1/updates/check` | 检查版本；可加载历史清单 |
| `POST /v1/updates/review` | Core 下载、安装或数据恢复确认 |
| `POST /v1/updates/download` | 下载或准备所选版本 |
| `POST /v1/updates/import` | 校验本地 ZIP 与同名 JSON |
| `POST /v1/updates/apply` | 安装准备好的版本 |
| `POST /v1/updates/recover` | 恢复中断事务 |
| `POST /v1/updates/finish` | 更新任务结束并恢复运行状态 |
| `POST /v1/updates/config` | 更新源及自动检查设置 |
| `POST /v1/updates/source` | Core/manager 源码位置与修改情况 |
| `POST /v1/updates/source-backup` | Core/manager 源码快照 |

源码接口请求中的 `component` 为 `core` 或 `manager`。快照只有 `snapshot.json` 的 `state=complete` 才表示完整；正在编辑导致校验失败时保留未完成材料，不替换源码。

开发命令在 `dev` 下执行。默认构建输出为工作区 `cache/builds`，隔离测试为 `cache/tests`，工具链为 `cache/tools`；不再使用根目录的 `dist`、`tmp` 或兼容链接。
