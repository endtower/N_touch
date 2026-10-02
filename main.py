#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Abyss Core - 主入口

FastAPI 应用本身零业务路由，职责有两个：
1. lifespan 启动 SystemCore（大脑 + 各通道；Web 业务路由在 adapt/web，端口 5002）
2. 作为将来系统管理 API（supervisor 状态/日志端点）的宿主

启动命令：
    python main.py           # 生产模式（无热重载）
    python main.py --dev     # 开发模式（文件修改自动重启，已排除日志/数据目录）
    可选 --host / --port 覆盖监听地址（默认 127.0.0.1:5000）

运行环境：Python 3.10+
"""

import argparse
import sys
import time
import uvicorn
from pathlib import Path
from fastapi import FastAPI
from typing import Dict, Any, AsyncIterator
from dotenv import load_dotenv

# 项目根 = main.py 所在目录。插入到 sys.path[0] 保证包导入不被环境干扰
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

# 配置外置入口（.env.dev 不存在时静默跳过；见 config/ 下的读取方）
load_dotenv(project_root / ".env.dev")

# 导入日志模块
from utils.logger import setup_logging

# 导入系统核心协调器
from core.system_core import SystemCore

# 初始化日志记录器
logger = setup_logging('main')


# 使用FastAPI推荐的lifespan事件处理器
async def lifespan(app: FastAPI) -> AsyncIterator[Dict[str, Any]]:
    """FastAPI应用生命周期管理"""
    logger.info("开始初始化系统核心组件...")

    # 初始化SystemCore（核心组件）
    async with SystemCore() as system_core:
        # 明确将核心组件绑定到app.state
        app.state.system_core = system_core
        app.state.started_at = time.time()
        logger.info("SystemCore初始化完成并绑定到应用状态")

        # 让应用运行
        yield {}


# 创建FastAPI应用（零路由：目前只承载 lifespan；将来在此挂管理 API）
app = FastAPI(title="Abyss Core", description="消息处理核心服务",
              version="1.0", lifespan=lifespan)

logger.info("应用初始化完成")

# ======================================
# 主程序入口
# ======================================

def main() -> None:
    """主函数：启动FastAPI服务"""
    parser = argparse.ArgumentParser(description="Abyss Core 主服务")
    parser.add_argument("--dev", action="store_true",
                        help="开发模式：开启热重载（生产/打包环境务必关闭）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    try:
        if args.dev:
            # 运行期写入热点必须从热重载监视中排除：服务启动即写日志（logs/），
            # 若被 watchfiles 监视到会触发「写入 → 重启 → 再写入」的无限重启风暴
            reload_excludes = [
                "logs", "logs/*", "*.log",           # 运行日志
                "tests", "tests/*",                   # 测试产物 results/*.json
                "docs", "docs/*",                     # 文档改动无需重启后端
                "scripts", "scripts/*",
                "web_frontend", "web_frontend/*",     # 前端由 Vite 自管
                "__pycache__", "*__pycache__*", "*.pyc",
                "*.db", "*.sqlite*", "*.sqlite3",     # 数据库文件
                "memory", "memory/*",                 # 运行期数据（消息/记忆/统计）
            ]
            uvicorn.run(
                "main:app", host=args.host, port=args.port, reload=True,
                reload_dirs=[str(project_root)],
                reload_excludes=reload_excludes,
            )
        else:
            # 生产模式：直接传 app 对象，无文件监视、无子进程。
            # 持有 Server 实例供 /system/core/shutdown|restart 优雅关闭
            config = uvicorn.Config(app, host=args.host, port=args.port)
            server = uvicorn.Server(config)
            app.state.uvicorn_server = server
            from adapt.web.system_api import set_uvicorn_server
            set_uvicorn_server(server)   # 注入给 aiohttp 侧的系统管理接口
            server.run()

    except KeyboardInterrupt:
        logger.info("接收到中断信号，正在停止服务...")


if __name__ == "__main__":
    main()
