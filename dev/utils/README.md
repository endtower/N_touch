# 通用工具层

通用工具层提供所有模块共用的基础工具函数，包括日志记录、LLM 客户端管理、历史/链上下文管理与错误处理等。

工程级 API 说明见 [docs/api/infrastructure.md](../docs/api/infrastructure.md)；启动与目录导航见 [docs/工程总览.md](../docs/工程总览.md)。

## 文件结构

- **logger.py**：日志工具，负责系统日志的记录与管理。
- **llm_client_manager.py**：LLM 客户端管理器，按提供方/模型创建与复用异步客户端。
- **history_manager.py**：历史管理器基类，管理会话上下文文件的读写与生命周期（链内 / 链外文件命名与归档）。
- **chain_context.py**：事件链上下文（contextvar）读写，供各 Agent 在链内传递当前链标识。
- **error_handling.py**：错误处理装饰器，提供统一的异步异常捕获机制。
- **__init__.py**：模块初始化文件。

## 主要功能

- 统一的日志记录机制
- LLM 客户端的创建与复用
- 会话历史与事件链上下文管理
- 错误捕获与处理
- 提供其他通用辅助函数

## 设计要点

- 工具函数应当是无状态的（history_manager 等有状态对象按单例/实例管理）
- 提供一致的接口和使用方式
- 注重性能和可重用性
- 易于扩展和维护

## 维护提示

按用户规则，重大关键方法的失败应直接上抛崩溃阻断，不做静默 try-except 降级（见项目根 [README.md](../README.md) 与 [docs/api/infrastructure.md](../docs/api/infrastructure.md)）。