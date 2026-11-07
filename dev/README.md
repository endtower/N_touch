# 弱链接 开发工程

此目录是开发源码。实际运行版位于同级 `../app/`，个人数据在 `../data/`。修改本目录不会自动修改已安装的程序。

`abyss-storage-dir.txt` 把开发模式的存储根固定到上一级；从任意工作目录启动开发入口时，均使用工作区的 `data/cache/external/backups`，不在开发源码中创建另一份个人数据。

## 工程组成

| 位置 | 职责 |
|---|---|
| `launcher/` | WPF 启动器；唯一打包的自有业务 EXE，也执行更新事务 |
| `manager_service.py`、`manager_core.py`、`manager_updates.py` | 本机管理接口、进程控制、安装与更新 |
| `main.py`、`core/` | Core 入口、生命周期与进程规格 |
| `ingress_service.py`、`adapt/` | QQ、网页与通用桥接的消息接入 |
| `brain/` | 注意力、决策、对话、情绪、人格与记忆业务 |
| `memory/`、`utils/` | 消息存储模块及公共服务；此处不存个人数据库 |
| `config/` | 配置加载与默认值；身份、密钥等从个人数据区加载 |
| `web_frontend/` | Vue 管理界面及匹配的编译资源 |
| `release_defaults/` | 公开源码包使用的通用默认模块 |
| `scripts/` | 构建、部署、迁移和隔离验收 |
| `tests/`、`updater-tests/` | 测试；历史个人测试资料仍不入库 |
| `updater/Installer.cs` | 编译进启动器的文件更新事务实现 |
| `releases/`、`component_versions.json` | 组件版本、兼容要求与发布说明 |
| `design/`、`assets/` | 设计令牌、规范和程序素材 |
| `docs/` | 存储规范与技术资料；历史私人文档在 `../private/docs/` |

## 构建与验收

以下为开发 Agent 使用的命令，工作目录为本目录；普通用户无需安装开发工具。

```powershell
python scripts/build_public_release.py --output ../cache/builds/public-<唯一名称> --feed-base-url <清单地址> --package-base-url <包地址> --metadata releases/core-<版本>.json
python scripts/acceptance_data_paths.py
python scripts/acceptance_source_layout.py --app ../app
```

输出目录必须是新目录，不能覆盖 `app` 或 `data`。本机工具链位于 `../cache/tools/`；构建产物在 `../cache/builds/`，隔离测试在 `../cache/tests/`。`.NET` 项目由 `Directory.Build.props` 统一设置输出位置。

公开构建生成一个启动器 EXE，以及管理服务、Core 的源码和匹配运行环境，不再构建独立业务 EXE。旧 PyInstaller 工程与独立更新器入口已归档到 `../private/legacy-build-tools/`，不属于当前构建流程。

安装、源码修改保护与恢复规则见 [updater/README.md](updater/README.md)；目录所有权见 [docs/STORAGE.md](docs/STORAGE.md)；用户操作指南见 [../AGENTS.md](../AGENTS.md)。

一键启动的源码脚本、链路等待、跳过与恢复规则，以及外部 AI 的配置接口见 [docs/STARTUP.md](docs/STARTUP.md)。`startup_plan.py` 是供 AI 直接修改的正式启动入口；打开启动器本身不执行业务启动。

仓库尚未因这次整理而发布。私人目录和本机运行区被忽略；公开前仍须单独审查 Git 历史与公共默认内容。
