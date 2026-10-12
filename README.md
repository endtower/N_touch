# N_Touch（Abyss Core）

一个类脑架构的 AI 聊天机器人后端：外部消息经适配层写入消息库，注意力系统以
**游标轮询**驱动 PFCA 决策层，再分发给对话代理；情绪系统（amy）与状态/记忆系统（hc）
贯穿全程。QQ 侧经 NapCat 系实现 SnowLuma（OneBot v11）接入，另带 Vue3 网页前端。

> 本仓库由 Abyss_Core0.71 干净副本逐步重构而来（内部代号 N_Touch 0.1）。

## 快速入口

| 你想… | 看这里 |
|---|---|
| **了解工程全貌（进程/运行/目录/构建）** | [docs/工程总览.md](docs/工程总览.md) — 工程实现主入口 |
| **了解项目与文档导航** | [docs/README.md](docs/README.md) — 项目概览 + 关键约束 + 全库索引 |
| **改视觉风格（配色 / 字体 / 形态 / 并行分工）** | [design/CMF.md](design/CMF.md) — 单一令牌源头 + 两端生成物 + 校验器 + 所有权边界 |
| **接入 QQ（SnowLuma）** | [docs/setup_snowluma.md](docs/setup_snowluma.md) — WebUI 配置步骤 + 自检 + 排错 |
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