# N_Touch（Abyss Core）

一个类脑架构的 AI 聊天机器人后端：外部消息经适配层写入消息库，注意力系统以
**游标轮询**驱动 PFCA 决策层，再分发给对话代理；情绪系统（amy）与状态/记忆系统（hc）
贯穿全程。QQ 侧经 NapCat（OneBot v11）接入，另带 Vue3 网页前端。

> 本仓库由 Abyss_Core0.71 干净副本逐步重构而来（内部代号 N_Touch 0.1）。

## 快速入口

| 你想… | 看这里 |
|---|---|
| **了解项目与文档导航** | [docs/README.md](docs/README.md) — 项目概览 + 关键约束 + 全库索引 |
| **接入 QQ（SnowLuma）** | [docs/setup_snowluma.md](docs/setup_snowluma.md) — WebUI 配置步骤 + 自检 + 排错 |
| 查某个模块的 API | [docs/api/](docs/api/README.md) — 按代码包 1:1 对应的 API 参考 |
| 查已完成 / 待办 / 现象 / 远景 | [docs/已完成.md](docs/已完成.md) · [docs/待办.md](docs/待办.md) · [docs/现象观察.md](docs/现象观察.md) · [docs/未来改进.md](docs/未来改进.md) |
| 跑测试 | [docs/测试系统使用说明.md](docs/测试系统使用说明.md) |

## 目录结构（2026-08 重构后）

```
main.py               FastAPI 入口（lifespan 启动 SystemCore；注意：本身零业务路由）
core/                 SystemCore + ServiceRegistry 生命周期
adapt/                外部适配层：web(前端 5002)、onebot(NapCat 5001)、出站路由
memory/               消息入库与游标消费（chat_history.db）
brain/
  att/                注意力：station 仲裁 + 4 个 truck + proactive 后台循环
  pfc/                决策层（PFCA）
  chat/               对话代理（conversation / social_chat）
  amy/                情绪系统（EmotionalInterface 统一门面）
  hc/                 全局状态 + SQLite 长期记忆库（memory.db）+ 临时记忆
  base/               HeavyCore / HeavySession / ToolManager 等框架基类
utils/                日志 / 错误处理 / LLM 客户端 / 动态提示词等共享服务
web_frontend/         Vue3 前端（WS 契约权威定义在 src/types.ts）
tests/                冒烟测试 + 预设测试环境
scripts/              迁移与校验脚本（check_api_docs.py 文档漂移校验）
docs/                 文档（入口索引 + 四份状态文档 + api/ 活文档 + 指南）
```

## 端口

| 端口 | 组件 |
|---|---|
| 5000 | main.py FastAPI（仅承载 lifespan，无业务路由） |
| 5001 | OneBot v11 反向 WS 服务端（SnowLuma/NapCat 连入） |
| 5002 | WebChannel：对前端的全部 HTTP + WS 接口 |
| 5173 | web_frontend Vite 开发服（代理到 5002） |

## 启动

```bash
python main.py --dev             # 后端完整版（热重载；生产用 python main.py）
python scripts/web_only.py       # 只读模式（不起大脑，查看上下文/记忆等）
cd web_frontend && npm run dev   # 前端开发服；非开发时可直接开 5002（后端伺服 dist）
```
