# 弱链接 / WeakLink

双击 **[启动弱链接.cmd](启动弱链接.cmd)**，或打开 [app/AbyssLauncher.exe](app/AbyssLauncher.exe)。启动器统一管理 Core、消息接入和 SnowLuma，并提供各项目的管理页面。

## 目录导航

| 目录 | 内容 |
|---|---|
| `app/` | 正在使用的启动器、各组件源码和自带运行环境 |
| `data/` | 个人配置、记忆、人格、会话、附件、日志和界面设置；不要删除或移动 |
| `external/` | 第三方程序；内部配置、登录状态和数据位置遵循上游 |
| `dev/` | 开发源码、公共默认值、构建脚本、测试与发布说明 |
| `cache/` | 下载、构建、隔离测试和工具链；旧产物暂存于 `legacy-*` 中 |
| `backups/` | 更新、源码、历史数据与本次目录整理的备份 |
| `private/` | 私人资料、历史交接、参考图、实验项目和旧构建工具 |

唯一打包的自有业务程序是 `app/AbyssLauncher.exe`；管理服务、Core 和消息接入以源码运行。Python、Node 等上游运行环境随程序提供。

## 给接手的 Agent

先读 [AGENTS.md](AGENTS.md)。通过 `GET /v1/storage` 确认真实运行位置；管理服务未启动时，检查 `app/abyss-data-dir.txt`、`app/abyss-storage-dir.txt` 和组件目录。

- 修改用户正在使用的功能：操作 `app/components/<组件>/source/`，先备份，修改后重启对应服务。
- 开发公开版本与构建安装包：操作 `dev/`，入口见 [dev/README.md](dev/README.md)。开发源码不会自动覆盖运行源码。
- 用户数据始终归 `data/`；SnowLuma 安装在 `external/snowluma/`，不接管其内部存储。
- 本地源码修改保护继续有效；不能通过重置安装基线绕过保护。

完整存储规则见 [dev/docs/STORAGE.md](dev/docs/STORAGE.md)。本次整理不修改数据库内容、人格、嵌入模型或第三方内部配置。
